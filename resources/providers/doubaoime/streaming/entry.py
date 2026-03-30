#!/usr/bin/env python3

import base64
import ctypes
import ctypes.util
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
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

REGISTER_URL = "https://log.snssdk.com/service/2/device_register/"
SETTINGS_URL = "https://is.snssdk.com/service/settings/v3/"
DEFAULT_URL = "wss://frontier-audio-ime-ws.doubao.com/ocean/api/v1/ws"
DEFAULT_AID = "401734"
DEFAULT_TIMEOUT = 30
DEFAULT_FINISH_GRACE_SECS = 0.4
DEFAULT_FRAME_DURATION_MS = 20
DEFAULT_SAMPLE_RATE = 16000
DEFAULT_CHANNELS = 1
DEFAULT_APP_NAME = "com.android.chrome"
DEFAULT_CREDENTIAL_PATH = "~/.cache/vinput/doubaoime-asr/credentials.json"
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
EXIT_RUNTIME_ERROR = 1
EXIT_USAGE_ERROR = 2

USER_AGENT = (
    "com.bytedance.android.doubaoime/100102018 "
    "(Linux; U; Android 16; en_US; Pixel 7 Pro; "
    "Build/BP2A.250605.031.A2; Cronet/TTNetVersion:94cf429a "
    "2025-11-17 QuicVersion:1f89f732 2025-05-08)"
)

APP_CONFIG = {
    "aid": 401734,
    "app_name": "oime",
    "version_code": 100102018,
    "version_name": "1.1.2",
    "manifest_version_code": 100102018,
    "update_version_code": 100102018,
    "channel": "official",
    "package": "com.bytedance.android.doubaoime",
}

DEFAULT_DEVICE_CONFIG = {
    "device_platform": "android",
    "os": "android",
    "os_api": "34",
    "os_version": "16",
    "device_type": "Pixel 7 Pro",
    "device_brand": "google",
    "device_model": "Pixel 7 Pro",
    "resolution": "1080*2400",
    "dpi": "420",
    "language": "zh",
    "timezone": 8,
    "access": "wifi",
    "rom": "UP1A.231005.007",
    "rom_version": "UP1A.231005.007",
}

FRAME_STATE_FIRST = 1
FRAME_STATE_MIDDLE = 3
FRAME_STATE_LAST = 9
OPUS_APPLICATION_AUDIO = 2049
OPUS_MAX_PACKET_SIZE = 4000


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


def json_dumps_compact(data: Dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def encode_varint(value: int) -> bytes:
    if value < 0:
        value += 1 << 64
    out = bytearray()
    while True:
        to_write = value & 0x7F
        value >>= 7
        if value:
            out.append(to_write | 0x80)
        else:
            out.append(to_write)
            return bytes(out)


def decode_varint(data: bytes, index: int) -> tuple[int, int]:
    shift = 0
    value = 0
    while True:
        if index >= len(data):
            raise ValueError("Unexpected end of protobuf varint.")
        byte = data[index]
        index += 1
        value |= (byte & 0x7F) << shift
        if not (byte & 0x80):
            return value, index
        shift += 7
        if shift >= 64:
            raise ValueError("Protobuf varint is too long.")


def encode_field_bytes(field_number: int, value: bytes) -> bytes:
    return encode_varint((field_number << 3) | 2) + encode_varint(len(value)) + value


def encode_field_string(field_number: int, value: str) -> bytes:
    return encode_field_bytes(field_number, value.encode("utf-8"))


def encode_field_int(field_number: int, value: int) -> bytes:
    return encode_varint((field_number << 3) | 0) + encode_varint(value)


def parse_protobuf_fields(data: bytes) -> Dict[int, Any]:
    fields: Dict[int, Any] = {}
    index = 0
    while index < len(data):
        key, index = decode_varint(data, index)
        field_number = key >> 3
        wire_type = key & 0x07
        if wire_type == 0:
            value, index = decode_varint(data, index)
        elif wire_type == 1:
            if index + 8 > len(data):
                raise ValueError("Unexpected end of fixed64 field.")
            value = data[index : index + 8]
            index += 8
        elif wire_type == 2:
            length, index = decode_varint(data, index)
            if index + length > len(data):
                raise ValueError("Unexpected end of length-delimited field.")
            value = data[index : index + length]
            index += length
        elif wire_type == 5:
            if index + 4 > len(data):
                raise ValueError("Unexpected end of fixed32 field.")
            value = data[index : index + 4]
            index += 4
        else:
            raise ValueError(f"Unsupported protobuf wire type: {wire_type}")

        if field_number in fields:
            existing = fields[field_number]
            if isinstance(existing, list):
                existing.append(value)
            else:
                fields[field_number] = [existing, value]
        else:
            fields[field_number] = value
    return fields


def get_proto_string(fields: Dict[int, Any], field_number: int) -> str:
    value = fields.get(field_number, b"")
    if isinstance(value, list):
        value = value[-1]
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, int):
        return str(value)
    return ""


def get_proto_int(fields: Dict[int, Any], field_number: int) -> int:
    value = fields.get(field_number, 0)
    if isinstance(value, list):
        value = value[-1]
    if isinstance(value, int):
        return value
    return 0


@dataclass
class DeviceCredentials:
    device_id: str = ""
    install_id: str = ""
    cdid: str = ""
    openudid: str = ""
    clientudid: str = ""
    token: str = ""

    def to_dict(self) -> Dict[str, str]:
        return {
            "device_id": self.device_id,
            "install_id": self.install_id,
            "cdid": self.cdid,
            "openudid": self.openudid,
            "clientudid": self.clientudid,
            "token": self.token,
        }


@dataclass
class SessionState:
    session_started: bool = False
    error: Optional[str] = None
    closed: bool = False
    finished: bool = False
    committed_text: str = ""
    current_partial_text: str = ""
    last_final_text: str = ""

    def has_usable_final(self) -> bool:
        return bool(normalize_transcript_text(self.last_final_text))

    def get_visible_text(self) -> str:
        return normalize_transcript_text(
            combine_transcript(self.committed_text, self.current_partial_text)
        )

    def record_partial(self, text: str) -> str:
        self.current_partial_text = normalize_transcript_text(text)
        return self.get_visible_text()

    def record_final(self, text: str) -> str:
        full_text = normalize_transcript_text(
            combine_transcript(self.committed_text, text)
        )
        self.committed_text = full_text
        self.current_partial_text = ""
        self.last_final_text = full_text
        return full_text


class OpusEncoder:
    def __init__(self, sample_rate: int, channels: int) -> None:
        candidates = []
        discovered = ctypes.util.find_library("opus")
        if discovered:
            candidates.append(discovered)
        candidates.extend(["libopus.so.0", "libopus.so"])

        self.lib = None
        for candidate in candidates:
            try:
                self.lib = ctypes.CDLL(candidate)
                break
            except OSError:
                continue
        if self.lib is None:
            raise RuntimeError("Failed to locate system libopus.")
        self.lib.opus_encoder_create.argtypes = [
            ctypes.c_int32,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_int),
        ]
        self.lib.opus_encoder_create.restype = ctypes.c_void_p
        self.lib.opus_encode.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_int16),
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_ubyte),
            ctypes.c_int32,
        ]
        self.lib.opus_encode.restype = ctypes.c_int32
        self.lib.opus_encoder_destroy.argtypes = [ctypes.c_void_p]
        self.lib.opus_encoder_destroy.restype = None

        error = ctypes.c_int()
        self.encoder = self.lib.opus_encoder_create(
            sample_rate,
            channels,
            OPUS_APPLICATION_AUDIO,
            ctypes.byref(error),
        )
        if not self.encoder or error.value != 0:
            raise RuntimeError(f"libopus encoder init failed: {error.value}")

    def encode(self, pcm_frame: bytes, samples_per_frame: int) -> bytes:
        pcm_array = (ctypes.c_int16 * samples_per_frame).from_buffer_copy(pcm_frame)
        output = (ctypes.c_ubyte * OPUS_MAX_PACKET_SIZE)()
        encoded_size = self.lib.opus_encode(
            self.encoder,
            pcm_array,
            samples_per_frame,
            output,
            OPUS_MAX_PACKET_SIZE,
        )
        if encoded_size < 0:
            raise RuntimeError(f"libopus encode failed: {encoded_size}")
        return bytes(output[:encoded_size])

    def __del__(self) -> None:
        encoder = getattr(self, "encoder", None)
        lib = getattr(self, "lib", None)
        if encoder and lib:
            try:
                lib.opus_encoder_destroy(encoder)
            except Exception:
                pass


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

    def send_binary(self, payload: bytes) -> None:
        self._send_frame(0x2, payload)

    def recv_binary(self) -> Optional[bytes]:
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
            if opcode not in {0x0, 0x2}:
                continue

            if opcode == 0x2:
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


def generate_openudid() -> str:
    return secrets.token_hex(8)


def generate_uuid() -> str:
    return str(uuid.uuid4())


def http_post_json(
    url: str,
    *,
    params: Dict[str, Any],
    body: Any,
    headers: Dict[str, str],
    timeout: int,
) -> Dict[str, Any]:
    full_url = url + "?" + urlencode(params)
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = Request(full_url, data=payload, headers=headers, method="POST")
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def http_post_form(
    url: str,
    *,
    params: Dict[str, Any],
    body: str,
    headers: Dict[str, str],
    timeout: int,
) -> Dict[str, Any]:
    full_url = url + "?" + urlencode(params)
    request = Request(full_url, data=body.encode("utf-8"), headers=headers, method="POST")
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def load_credentials(path: Path) -> Optional[DeviceCredentials]:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return DeviceCredentials(
        device_id=str(data.get("device_id", "")),
        install_id=str(data.get("install_id", "")),
        cdid=str(data.get("cdid", "")),
        openudid=str(data.get("openudid", "")),
        clientudid=str(data.get("clientudid", "")),
        token=str(data.get("token", "")),
    )


def save_credentials(path: Path, credentials: DeviceCredentials) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(credentials.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def register_device(timeout: int) -> DeviceCredentials:
    cdid = generate_uuid()
    openudid = generate_openudid()
    clientudid = generate_uuid()

    header = {
        "device_id": 0,
        "install_id": 0,
        **APP_CONFIG,
        **DEFAULT_DEVICE_CONFIG,
        "openudid": openudid,
        "clientudid": clientudid,
        "cdid": cdid,
        "region": "CN",
        "tz_name": "Asia/Shanghai",
        "tz_offset": 28800,
        "sim_region": "cn",
        "carrier_region": "cn",
        "cpu_abi": "arm64-v8a",
        "build_serial": "unknown",
        "not_request_sender": 0,
        "sig_hash": "",
        "google_aid": "",
        "mc": "",
        "serial_number": "",
    }
    body = {
        "magic_tag": "ss_app_log",
        "header": header,
        "_gen_time": int(time.time() * 1000),
    }
    params = {
        "device_platform": "android",
        "os": "android",
        "ssmix": "a",
        "_rticket": int(time.time() * 1000),
        "cdid": cdid,
        "channel": APP_CONFIG["channel"],
        "aid": str(APP_CONFIG["aid"]),
        "app_name": APP_CONFIG["app_name"],
        "version_code": str(APP_CONFIG["version_code"]),
        "version_name": APP_CONFIG["version_name"],
        "manifest_version_code": str(APP_CONFIG["manifest_version_code"]),
        "update_version_code": str(APP_CONFIG["update_version_code"]),
        "resolution": DEFAULT_DEVICE_CONFIG["resolution"],
        "dpi": DEFAULT_DEVICE_CONFIG["dpi"],
        "device_type": DEFAULT_DEVICE_CONFIG["device_type"],
        "device_brand": DEFAULT_DEVICE_CONFIG["device_brand"],
        "language": DEFAULT_DEVICE_CONFIG["language"],
        "os_api": DEFAULT_DEVICE_CONFIG["os_api"],
        "os_version": DEFAULT_DEVICE_CONFIG["os_version"],
        "ac": "wifi",
    }
    headers = {
        "User-Agent": USER_AGENT,
        "Content-Type": "application/json",
    }
    response = http_post_json(
        REGISTER_URL,
        params=params,
        body=body,
        headers=headers,
        timeout=timeout,
    )

    device_id = str(response.get("device_id", "") or response.get("device_id_str", ""))
    install_id = str(response.get("install_id", "") or response.get("install_id_str", ""))
    if not device_id or device_id == "0":
        raise RuntimeError("Doubao IME device registration failed.")

    return DeviceCredentials(
        device_id=device_id,
        install_id=install_id,
        cdid=cdid,
        openudid=openudid,
        clientudid=clientudid,
    )


def get_asr_token(device_id: str, cdid: str, timeout: int) -> str:
    body = "body=null"
    params = {
        "device_platform": "android",
        "os": "android",
        "ssmix": "a",
        "_rticket": str(int(time.time() * 1000)),
        "cdid": cdid,
        "channel": APP_CONFIG["channel"],
        "aid": str(APP_CONFIG["aid"]),
        "app_name": APP_CONFIG["app_name"],
        "version_code": str(APP_CONFIG["version_code"]),
        "version_name": APP_CONFIG["version_name"],
        "device_id": device_id,
    }
    headers = {
        "User-Agent": USER_AGENT,
        "x-ss-stub": hashlib.md5(body.encode("utf-8")).hexdigest().upper(),
        "Content-Type": "application/x-www-form-urlencoded",
    }
    response = http_post_form(
        SETTINGS_URL,
        params=params,
        body=body,
        headers=headers,
        timeout=timeout,
    )
    return str(response["data"]["settings"]["asr_config"]["app_key"])


def ensure_credentials(timeout: int) -> DeviceCredentials:
    credential_path = Path(
        os.path.expanduser(
            get_optional_env("VINPUT_ASR_CREDENTIAL_PATH", DEFAULT_CREDENTIAL_PATH)
        )
    )
    credentials = load_credentials(credential_path) or DeviceCredentials()

    env_device_id = get_optional_env("VINPUT_ASR_DEVICE_ID")
    env_token = get_optional_env("VINPUT_ASR_TOKEN")
    if env_device_id:
        credentials.device_id = env_device_id
    if env_token:
        credentials.token = env_token

    if not credentials.device_id:
        credentials = register_device(timeout)
    if not credentials.cdid:
        credentials.cdid = generate_uuid()
    if not credentials.token:
        credentials.token = get_asr_token(credentials.device_id, credentials.cdid, timeout)

    save_credentials(credential_path, credentials)
    return credentials


def build_websocket_url(device_id: str) -> str:
    base_url = get_optional_env("VINPUT_ASR_URL", DEFAULT_URL)
    parsed = urlparse(base_url)
    query = {"aid": get_optional_env("VINPUT_ASR_AID", DEFAULT_AID), "device_id": device_id}
    if parsed.query:
        for part in parsed.query.split("&"):
            if not part:
                continue
            if "=" in part:
                key, value = part.split("=", 1)
            else:
                key, value = part, ""
            query.setdefault(key, value)
    query_string = urlencode(query)
    path = parsed.path or "/"
    return f"{parsed.scheme}://{parsed.netloc}{path}?{query_string}"


def build_start_task(request_id: str, token: str) -> bytes:
    payload = bytearray()
    payload.extend(encode_field_string(2, token))
    payload.extend(encode_field_string(3, "ASR"))
    payload.extend(encode_field_string(5, "StartTask"))
    payload.extend(encode_field_string(8, request_id))
    return bytes(payload)


def build_start_session(request_id: str, token: str, config_json: str) -> bytes:
    payload = bytearray()
    payload.extend(encode_field_string(2, token))
    payload.extend(encode_field_string(3, "ASR"))
    payload.extend(encode_field_string(5, "StartSession"))
    payload.extend(encode_field_string(6, config_json))
    payload.extend(encode_field_string(8, request_id))
    return bytes(payload)


def build_finish_session(request_id: str, token: str) -> bytes:
    payload = bytearray()
    payload.extend(encode_field_string(2, token))
    payload.extend(encode_field_string(3, "ASR"))
    payload.extend(encode_field_string(5, "FinishSession"))
    payload.extend(encode_field_string(8, request_id))
    return bytes(payload)


def build_asr_request(
    audio_data: bytes,
    request_id: str,
    frame_state: int,
    timestamp_ms: int,
) -> bytes:
    metadata = json_dumps_compact({"extra": {}, "timestamp_ms": timestamp_ms})
    payload = bytearray()
    payload.extend(encode_field_string(3, "ASR"))
    payload.extend(encode_field_string(5, "TaskRequest"))
    payload.extend(encode_field_string(6, metadata))
    payload.extend(encode_field_bytes(7, audio_data))
    payload.extend(encode_field_string(8, request_id))
    payload.extend(encode_field_int(9, frame_state))
    return bytes(payload)


def build_session_config(device_id: str) -> str:
    config = {
        "audio_info": {
            "channel": DEFAULT_CHANNELS,
            "format": "speech_opus",
            "sample_rate": DEFAULT_SAMPLE_RATE,
        },
        "enable_punctuation": get_optional_bool_env("VINPUT_ASR_ENABLE_PUNCTUATION", True),
        "enable_speech_rejection": get_optional_bool_env(
            "VINPUT_ASR_ENABLE_SPEECH_REJECTION", False
        ),
        "extra": {
            "app_name": get_optional_env("VINPUT_ASR_APP_NAME", DEFAULT_APP_NAME),
            "cell_compress_rate": 8,
            "did": device_id,
            "enable_asr_threepass": get_optional_bool_env(
                "VINPUT_ASR_ENABLE_ASR_THREEPASS", True
            ),
            "enable_asr_twopass": get_optional_bool_env(
                "VINPUT_ASR_ENABLE_ASR_TWOPASS", True
            ),
            "input_mode": "tool",
        },
    }
    return json_dumps_compact(config)


def emit_final_text(
    state: SessionState,
    text: str,
    *,
    utterance_final: bool = False,
    words: Optional[list] = None,
    allow_same_text: bool = False,
) -> bool:
    final_text = normalize_transcript_text(text)
    if not final_text:
        return False
    if not allow_same_text and final_text == normalize_transcript_text(state.last_final_text):
        return False

    recorded = state.record_final(final_text)
    if isinstance(words, list) and words:
        write_stdout(
            {
                "type": "final_timestamps",
                "text": recorded,
                "segment_final": True,
                "utterance_final": utterance_final,
                "words": words,
            }
        )
    else:
        write_stdout(
            {
                "type": "final",
                "text": recorded,
                "segment_final": True,
                "utterance_final": utterance_final,
            }
        )
    return True


def emit_fallback_final(state: SessionState) -> bool:
    visible_text = state.get_visible_text()
    if not visible_text:
        return False

    if normalize_transcript_text(visible_text) == normalize_transcript_text(
        state.last_final_text
    ):
        write_stdout(
            {
                "type": "final",
                "text": normalize_transcript_text(state.last_final_text),
                "segment_final": True,
                "utterance_final": True,
            }
        )
        return True

    return emit_final_text(
        state,
        visible_text,
        utterance_final=True,
        allow_same_text=True,
    )


def extract_words(results: list) -> Optional[list]:
    for result in results:
        if not isinstance(result, dict):
            continue
        words = result.get("words")
        if isinstance(words, list) and words:
            return words
        alternatives = result.get("alternatives")
        if not isinstance(alternatives, list):
            continue
        for item in alternatives:
            if not isinstance(item, dict):
                continue
            words = item.get("words")
            if isinstance(words, list) and words:
                return words
    return None


def parse_server_response(message: bytes) -> Dict[str, Any]:
    fields = parse_protobuf_fields(message)
    message_type = get_proto_string(fields, 4)
    result_json = get_proto_string(fields, 7)
    parsed: Dict[str, Any] = {
        "message_type": message_type,
        "status_code": get_proto_int(fields, 5),
        "status_message": get_proto_string(fields, 6),
    }
    if result_json:
        try:
            parsed["result"] = json.loads(result_json)
        except json.JSONDecodeError:
            parsed["result"] = None
    else:
        parsed["result"] = None
    return parsed


def handle_server_message(message: bytes, state: SessionState, request_id: str) -> None:
    parsed = parse_server_response(message)
    message_type = parsed["message_type"]

    if message_type == "TaskStarted":
        return
    if message_type == "SessionStarted":
        if not state.session_started:
            write_stdout({"type": "session_started", "session_id": request_id})
            state.session_started = True
        return
    if message_type == "SessionFinished":
        state.finished = True
        return
    if message_type in {"TaskFailed", "SessionFailed"}:
        error_message = parsed["status_message"] or f"{message_type} ({parsed['status_code']})"
        if state.has_usable_final():
            write_stderr(f"Doubao IME terminal error ignored after final result: {error_message}")
            state.finished = True
            return
        state.error = error_message
        write_stdout({"type": "error", "message": error_message})
        state.finished = True
        return

    result = parsed["result"]
    if not isinstance(result, dict):
        return
    results = result.get("results")
    if not isinstance(results, list):
        return

    text = ""
    is_interim = True
    vad_finished = False
    nonstream_result = False
    for item in results:
        if not isinstance(item, dict):
            continue
        candidate_text = item.get("text")
        if isinstance(candidate_text, str) and candidate_text.strip():
            text = candidate_text
        if item.get("is_interim") is False:
            is_interim = False
        if item.get("is_vad_finished"):
            vad_finished = True
        extra = item.get("extra")
        if isinstance(extra, dict) and extra.get("nonstream_result"):
            nonstream_result = True

    if not text:
        return

    if nonstream_result or (not is_interim and vad_finished):
        emit_final_text(state, text, words=extract_words(results))
        return

    write_stdout({"type": "partial", "text": state.record_partial(text)})


def send_audio_frame(
    client: WebSocketClient,
    encoder: OpusEncoder,
    request_id: str,
    pcm_frame: bytes,
    frame_state: int,
    frame_index: int,
    samples_per_frame: int,
    start_timestamp_ms: int,
) -> int:
    opus_frame = encoder.encode(pcm_frame, samples_per_frame)
    timestamp_ms = start_timestamp_ms + frame_index * DEFAULT_FRAME_DURATION_MS
    client.send_binary(build_asr_request(opus_frame, request_id, frame_state, timestamp_ms))
    return frame_index + 1


def run() -> int:
    timeout = get_optional_int_env("VINPUT_ASR_TIMEOUT", DEFAULT_TIMEOUT)
    finish_grace_secs = get_optional_float_env(
        "VINPUT_ASR_FINISH_GRACE_SECS", DEFAULT_FINISH_GRACE_SECS
    )
    credentials = ensure_credentials(timeout)
    request_id = str(uuid.uuid4())
    token = credentials.token

    client = WebSocketClient(
        build_websocket_url(credentials.device_id),
        {
            "User-Agent": USER_AGENT,
            "proto-version": "v2",
            "x-custom-keepalive": "true",
        },
        timeout,
    )

    client.send_binary(build_start_task(request_id, token))
    start_task_response = client.recv_binary()
    if start_task_response is None:
        raise RuntimeError("Doubao IME websocket closed before task start.")

    state = SessionState()
    handle_server_message(start_task_response, state, request_id)

    client.send_binary(
        build_start_session(request_id, token, build_session_config(credentials.device_id))
    )
    start_session_response = client.recv_binary()
    if start_session_response is None:
        raise RuntimeError("Doubao IME websocket closed before session start.")
    handle_server_message(start_session_response, state, request_id)
    if state.error:
        return EXIT_RUNTIME_ERROR

    stop_event = threading.Event()

    def reader() -> None:
        try:
            while not stop_event.is_set():
                message = client.recv_binary()
                if message is None:
                    break
                handle_server_message(message, state, request_id)
                if state.finished:
                    break
        except Exception as exc:
            if not stop_event.is_set() and not state.has_usable_final():
                state.error = str(exc)
                write_stdout({"type": "error", "message": str(exc)})
            elif not stop_event.is_set():
                write_stderr(f"Doubao IME terminal exception ignored after final result: {exc}")
        finally:
            stop_event.set()

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()

    encoder = OpusEncoder(DEFAULT_SAMPLE_RATE, DEFAULT_CHANNELS)
    samples_per_frame = DEFAULT_SAMPLE_RATE * DEFAULT_FRAME_DURATION_MS // 1000
    bytes_per_frame = samples_per_frame * 2
    pcm_buffer = bytearray()
    frame_index = 0
    start_timestamp_ms = int(time.time() * 1000)
    sent_audio = False
    finish_requested = False

    def finish_session() -> None:
        nonlocal frame_index, sent_audio, finish_requested
        if finish_requested:
            return
        if pcm_buffer:
            padded = bytes(pcm_buffer)
            if len(padded) < bytes_per_frame:
                padded += b"\x00" * (bytes_per_frame - len(padded))
            frame_state = FRAME_STATE_FIRST if frame_index == 0 else FRAME_STATE_LAST
            frame_index = send_audio_frame(
                client,
                encoder,
                request_id,
                padded,
                frame_state,
                frame_index,
                samples_per_frame,
                start_timestamp_ms,
            )
            pcm_buffer.clear()
            sent_audio = True
        elif sent_audio:
            silent = b"\x00" * bytes_per_frame
            frame_state = FRAME_STATE_FIRST if frame_index == 0 else FRAME_STATE_LAST
            frame_index = send_audio_frame(
                client,
                encoder,
                request_id,
                silent,
                frame_state,
                frame_index,
                samples_per_frame,
                start_timestamp_ms,
            )
        client.send_binary(build_finish_session(request_id, token))
        finish_requested = True

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
                pcm_chunk = base64.b64decode(audio_base64)
                pcm_buffer.extend(pcm_chunk)

                while len(pcm_buffer) >= bytes_per_frame:
                    frame = bytes(pcm_buffer[:bytes_per_frame])
                    del pcm_buffer[:bytes_per_frame]
                    frame_state = FRAME_STATE_FIRST if frame_index == 0 else FRAME_STATE_MIDDLE
                    frame_index = send_audio_frame(
                        client,
                        encoder,
                        request_id,
                        frame,
                        frame_state,
                        frame_index,
                        samples_per_frame,
                        start_timestamp_ms,
                    )
                    sent_audio = True

                continue

            if event_type == "finish":
                finish_session()
                break

            if event_type == "cancel":
                stop_event.set()
                break

            raise ValueError(f"Unsupported event type: {event_type or '<missing>'}")
    finally:
        if finish_requested and not stop_event.is_set():
            thread.join(timeout=finish_grace_secs)
        stop_event.set()
        try:
            client.close()
        finally:
            thread.join(timeout=1.0)
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
