#!/usr/bin/env python3
"""Doubao IME (豆包输入法) duplex streaming speech-to-text provider via WebSocket protobuf protocol."""

from __future__ import annotations

import base64
import ctypes
import ctypes.util
import fcntl
import gzip
import hashlib
import json
import os
import secrets
import socket
import ssl
import struct
import sys
import tempfile
import threading
import time
import unicodedata
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
APP_KEY = "OrnqKvSSrs"
DEFAULT_IDENTITY = Path.home() / ".cache/vinput/doubaoime/credentials.json"
IDENTITY_FIELDS = ("device_id", "install_id", "cdid", "openudid", "clientudid")
PRESERVED = frozenset("]})>\u300b\u201d'\"\u3011\u2019\uff09%\u2103:\uff1a")

EXIT_SUCCESS = 0
EXIT_RUNTIME_ERROR = 1
EXIT_USAGE_ERROR = 2

MESSAGES: dict[str, dict[str, str]] = {
    "en_US": {
        "title": "Doubao IME Streaming ASR",
        "err_invalid_event": "Invalid input event: {detail}",
        "err_opus": "Doubao IME streaming: failed to load or initialize libopus: {detail}",
        "err_network": "Doubao IME streaming network error: {detail}",
        "err_upstream": "Doubao IME streaming server error {code}: {detail}",
        "err_runtime": "Doubao IME streaming runtime error: {detail}",
    },
    "zh_CN": {
        "title": "豆包输入法实时语音识别",
        "err_invalid_event": "输入事件格式错误：{detail}",
        "err_opus": "豆包输入法流式语音识别：加载或初始化 libopus 失败：{detail}",
        "err_network": "豆包输入法流式语音识别网络连接失败：{detail}",
        "err_upstream": "豆包输入法流式语音服务端报错 {code}：{detail}",
        "err_runtime": "豆包输入法流式语音运行异常：{detail}",
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


def remove_trailing_punctuation(text: str, *, per_line: bool = False) -> str:
    def trim(value: str) -> str:
        end = len(value)
        while end and value[end - 1] not in PRESERVED and unicodedata.category(value[end - 1]).startswith("P"):
            end -= 1
        return value[:end]

    if not per_line:
        return trim(text)
    lines = text.split("\n")
    for index, line in enumerate(lines):
        if index < len(lines) - 1 and line.endswith("\r"):
            lines[index] = trim(line[:-1]) + "\r"
        else:
            lines[index] = trim(line)
    return "\n".join(lines)


# AES cryptographic helpers for device registration
def _gf_mul(left: int, right: int) -> int:
    result = 0
    while right:
        if right & 1:
            result ^= left
        left = ((left << 1) ^ (0x11B if left & 0x80 else 0)) & 0xFF
        right >>= 1
    return result


def _gf_pow(value: int, exponent: int) -> int:
    result = 1
    while exponent:
        if exponent & 1:
            result = _gf_mul(result, value)
        value = _gf_mul(value, value)
        exponent >>= 1
    return result


def _sbox(value: int) -> int:
    inverse = 0 if value == 0 else _gf_pow(value, 254)
    result = inverse ^ ((inverse << 1) | (inverse >> 7)) ^ ((inverse << 2) | (inverse >> 6))
    result ^= ((inverse << 3) | (inverse >> 5)) ^ ((inverse << 4) | (inverse >> 4)) ^ 0x63
    return result & 0xFF


def _rot_word(word: list[int]) -> list[int]:
    return word[1:] + word[:1]


def _expand_key(key: bytes) -> list[list[int]]:
    if len(key) not in (16, 24, 32):
        raise ValueError("AES key must be 16, 24, or 32 bytes")
    nk = len(key) // 4
    rounds = nk + 6
    words = [list(key[index : index + 4]) for index in range(0, len(key), 4)]
    rcon = 1
    while len(words) < 4 * (rounds + 1):
        word = words[-1][:]
        if len(words) % nk == 0:
            word = [_sbox(item) for item in _rot_word(word)]
            word[0] ^= rcon
            rcon = _gf_mul(rcon, 2)
        elif nk > 6 and len(words) % nk == 4:
            word = [_sbox(item) for item in word]
        word = [word[index] ^ words[-nk][index] for index in range(4)]
        words.append(word)
    return [[byte for word in words[index : index + 4] for byte in word] for index in range(0, len(words), 4)]


def _aes_block(block: bytes, key: bytes) -> bytes:
    rounds = len(key) // 4 + 6
    round_keys = _expand_key(key)
    state = list(block)

    def add_round_key(round_key: list[int]) -> None:
        for index in range(16):
            state[index] ^= round_key[index]

    def substitute() -> None:
        for index in range(16):
            state[index] = _sbox(state[index])

    def shift_rows() -> None:
        original = state[:]
        for row in range(4):
            for column in range(4):
                state[4 * column + row] = original[4 * ((column + row) % 4) + row]

    def mix_columns() -> None:
        for column in range(4):
            offset = column * 4
            a = state[offset : offset + 4]
            state[offset] = _gf_mul(a[0], 2) ^ _gf_mul(a[1], 3) ^ a[2] ^ a[3]
            state[offset + 1] = a[0] ^ _gf_mul(a[1], 2) ^ _gf_mul(a[2], 3) ^ a[3]
            state[offset + 2] = a[0] ^ a[1] ^ _gf_mul(a[2], 2) ^ _gf_mul(a[3], 3)
            state[offset + 3] = _gf_mul(a[0], 3) ^ a[1] ^ a[2] ^ _gf_mul(a[3], 2)

    add_round_key(round_keys[0])
    for round_index in range(1, rounds):
        substitute()
        shift_rows()
        mix_columns()
        add_round_key(round_keys[round_index])
    substitute()
    shift_rows()
    add_round_key(round_keys[rounds])
    return bytes(state)


def _aes_cbc_encrypt(data: bytes, key: bytes, iv: bytes) -> bytes:
    if len(iv) != 16 or len(data) % 16:
        raise ValueError("AES-CBC requires a 16-byte IV and aligned data")
    result = bytearray()
    previous = iv
    for offset in range(0, len(data), 16):
        block = bytes(left ^ right for left, right in zip(data[offset : offset + 16], previous, strict=True))
        previous = _aes_block(block, key)
        result.extend(previous)
    return bytes(result)


def encode_tt_encrypt(gzip_bytes: bytes, salt: bytes | None = None) -> bytes:
    salt = secrets.token_bytes(32) if salt is None else salt
    if len(salt) != 32:
        raise ValueError("ttEncrypt salt must contain exactly 32 bytes")
    constant = bytes.fromhex(
        "4dd4c2e6b83162090e52b3c7a6733ba41cb2462b829ab58a196b39db57177524"
        "f49baf7f08e8d68d26a72e37c1a95a2f1f05a51892aef2949732b62a38aadd58"
    )
    digest = hashlib.sha512(hashlib.sha512(salt).digest() + constant).digest()
    key, iv = digest[:16], digest[16:32]
    payload = hashlib.sha512(gzip_bytes).digest() + gzip_bytes
    padding = 16 - len(payload) % 16
    payload += bytes([padding]) * padding
    return bytes.fromhex("746305100000") + salt + _aes_cbc_encrypt(payload, key, iv)


def new_installation() -> dict[str, Any]:
    return {
        "openudid": secrets.token_hex(10),
        "clientudid": str(uuid.uuid4()),
        "cdid": str(uuid.uuid4()),
        "first_install_time": int(time.time() * 1000),
    }


def registration_request(state: dict[str, Any]) -> dict[str, Any]:
    request_id = str(uuid.uuid4())
    header = {
        "aid": 401734,
        "app_name": "oime",
        "channel": "official",
        "package": "com.bytedance.android.doubaoime",
        "app_version": "1.5.1",
        "app_version_minor": "6",
        "version_code": 100501006,
        "update_version_code": 100501006,
        "manifest_version_code": 100501006,
        "not_request_sender": 1,
        "release_build": "",
        "custom": {},
        "device_platform": "android",
        "os": "Android",
        "os_version": "14",
        "os_api": 34,
        "device_model": "Python protocol test",
        "device_brand": "generic",
        "device_manufacturer": "generic",
        "cpu_abi": "arm64-v8a",
        "language": "zh",
        "timezone": 8,
        "tz_name": "Asia/Shanghai",
        "tz_offset": 28800,
        "access": "wifi",
        "openudid": state["openudid"],
        "clientudid": state["clientudid"],
        "cdid": state["cdid"],
        "git_hash": "b416399",
        "sdk_version_code": 4030263,
        "sdk_target_version": 30,
        "sdk_version": "4.3.2-rc.13",
        "sdk_flavor": "cnInner",
        "guest_mode": 0,
        "req_id": request_id,
        "apk_first_install_time": state["first_install_time"],
        "is_system_app": 0,
    }
    body = {"magic_tag": "ss_app_log", "_gen_time": int(time.time() * 1000), "header": header}
    return {
        "url": "https://log.snssdk.com/service/2/device_register/",
        "params": {"req_id": request_id, "tt_data": "a"},
        "headers": {"Content-Type": "application/octet-stream;tt-data=a"},
        "body": body,
    }


def register_device(request: dict[str, Any]) -> tuple[dict[str, str], dict[str, Any]]:
    raw = json.dumps(request["body"], ensure_ascii=False, separators=(",", ":")).encode()
    content = encode_tt_encrypt(gzip.compress(raw, mtime=0))
    target = request["url"] + "?" + urlencode(request["params"])
    with urlopen(Request(target, data=content, headers=request["headers"]), timeout=20) as response:
        body = json.loads(response.read())
    did = body.get("device_id_str") or str(body.get("device_id") or "")
    iid = body.get("install_id_str") or str(body.get("install_id") or "")
    if not did or not iid or did == "0" or iid == "0":
        raise RuntimeError("Registration did not return nonzero device and installation IDs")
    return {"device_id": did, "install_id": iid}, body


def save_identity(path: Path, state: dict[str, Any]) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(state, output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def ensure_identity(path: Path = DEFAULT_IDENTITY) -> dict[str, Any]:
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(path.suffix + ".lock").open("a") as lock:
        os.fchmod(lock.fileno(), 0o600)
        deadline = time.monotonic() + 30
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Identity lock deadline expired") from None
                time.sleep(0.05)
        if path.exists():
            state = json.loads(path.read_text(encoding="utf-8"))
        else:
            state = new_installation()
        if not isinstance(state, dict):
            raise ValueError("Identity cache must be a JSON object")
        if state.get("device_id") or state.get("install_id"):
            if not all(state.get(key) and str(state[key]) != "0" for key in IDENTITY_FIELDS):
                raise ValueError("Identity cache contains an incomplete or zero identity")
            if not path.exists():
                save_identity(path, state)
            path.chmod(0o600)
            return state
        if not all(state.get(key) for key in ("cdid", "openudid", "clientudid", "first_install_time")):
            raise ValueError("Identity cache contains an incomplete installation")
        save_identity(path, state)
        identifiers, _ = register_device(registration_request(state))
        state.update(identifiers)
        save_identity(path, state)
        return state


class OpusEncoder:
    def __init__(self, bitrate: int, custom_path: str = "") -> None:
        library = custom_path or os.getenv("VINPUT_ASR_LIBOPUS_PATH", "").strip() or ctypes.util.find_library("opus")
        if library is None:
            raise RuntimeError("libopus is required for speech_opus")
        self.lib = ctypes.CDLL(library)
        self.lib.opus_encoder_create.argtypes = [
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_int),
        ]
        self.lib.opus_encoder_create.restype = ctypes.c_void_p
        self.lib.opus_encoder_ctl.argtypes = [ctypes.c_void_p, ctypes.c_int]
        self.lib.opus_encoder_ctl.restype = ctypes.c_int
        self.lib.opus_encode.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_int16),
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_ubyte),
            ctypes.c_int32,
        ]
        self.lib.opus_encode.restype = ctypes.c_int32
        self.lib.opus_encoder_destroy.argtypes = [ctypes.c_void_p]
        error = ctypes.c_int()
        self.handle = self.lib.opus_encoder_create(16000, 1, 2049, ctypes.byref(error))
        if not self.handle or error.value:
            raise RuntimeError(f"Opus encoder creation failed: {error.value}")
        try:
            for request, value in [(4006, 0), (4002, bitrate), (4040, 5003)]:
                code = self.lib.opus_encoder_ctl(self.handle, request, ctypes.c_int(value))
                if code:
                    raise RuntimeError(f"Opus configuration failed: {code}")
        except BaseException:
            self.close()
            raise

    def encode(self, pcm: bytes) -> bytes:
        if not self.handle:
            raise RuntimeError("Opus encoder is closed")
        if len(pcm) != 320:
            raise ValueError("Opus frame must contain exactly 160 samples (10 ms)")
        samples = (ctypes.c_int16 * 160).from_buffer_copy(pcm)
        output = (ctypes.c_ubyte * 1275)()
        length = self.lib.opus_encode(self.handle, samples, 160, output, len(output))
        if length < 0:
            raise RuntimeError(f"Opus encoding failed: {length}")
        return bytes(output[:length])

    def close(self) -> None:
        if self.handle:
            self.lib.opus_encoder_destroy(self.handle)
            self.handle = None

    def __del__(self) -> None:
        self.close()


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


def parse_protobuf_fields(data: bytes) -> dict[int, Any]:
    proto_fields: dict[int, Any] = {}
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

        if field_number in proto_fields:
            existing = proto_fields[field_number]
            if isinstance(existing, list):
                existing.append(value)
            else:
                proto_fields[field_number] = [existing, value]
        else:
            proto_fields[field_number] = value
    return proto_fields


def get_proto_string(fields_map: dict[int, Any], field_number: int) -> str:
    value = fields_map.get(field_number, b"")
    if isinstance(value, list):
        value = value[-1]
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, int):
        return str(value)
    return ""


def get_proto_int(fields_map: dict[int, Any], field_number: int) -> int:
    value = fields_map.get(field_number, 0)
    if isinstance(value, list):
        value = value[-1]
    if isinstance(value, int):
        return value
    return 0


@dataclass
class WebSocketRequest:
    namespace: str = ""
    event: str = ""
    task_id: str = ""
    appkey: str = ""
    payload: str = ""
    audio: bytes = b""
    seq_id: int = 0

    def serialize_to_string(self) -> bytes:
        result = b""
        for number, value in (
            (2, self.appkey),
            (3, self.namespace),
            (5, self.event),
            (6, self.payload),
            (8, self.task_id),
        ):
            if value:
                result += encode_field_string(number, value)
        if self.audio:
            result += encode_field_bytes(7, self.audio)
        if self.seq_id:
            result += encode_field_int(9, self.seq_id)
        return result


@dataclass
class WebSocketResponse:
    task_id: str = ""
    event: str = ""
    status_code: int = 0
    status_text: str = ""
    payload: str = ""
    seq_id: int = 0

    @classmethod
    def from_string(cls, data: bytes) -> WebSocketResponse:
        proto_fields = parse_protobuf_fields(data)
        return cls(
            get_proto_string(proto_fields, 1),
            get_proto_string(proto_fields, 4),
            get_proto_int(proto_fields, 5),
            get_proto_string(proto_fields, 6),
            get_proto_string(proto_fields, 7),
            get_proto_int(proto_fields, 9),
        )


class WebSocketClient:
    def __init__(self, url: str, headers: dict[str, str], timeout: float) -> None:
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
        self._send_lock = threading.Lock()
        self.socket = self._connect()

    def _connect(self) -> socket.socket:
        raw_sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        raw_sock.settimeout(self.timeout)

        sock = raw_sock
        try:
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
        except BaseException:
            sock.close()
            raise

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
            raise RuntimeError(", ".join(details))

        accept = headers.get("sec-websocket-accept")
        expected = base64.b64encode(hashlib.sha1((key + GUID).encode("utf-8")).digest()).decode("ascii")
        if accept != expected:
            raise RuntimeError("WebSocket handshake failed: invalid Sec-WebSocket-Accept header.")
        self.response_headers = headers

    def close(self) -> None:
        with self._send_lock:
            if self._closed:
                return
            self._closed = True
            try:
                self.socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.socket.close()

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

            if len(fragments) > 4 * 1024 * 1024:
                raise ValueError("WebSocket message exceeds 4 MB")
            if not fin:
                continue

            if current_opcode is None:
                continue
            return current_opcode, bytes(fragments)

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        if self._closed:
            raise ConnectionError("WebSocket connection is closed")

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

        if length > 4 * 1024 * 1024:
            raise ValueError("WebSocket frame exceeds 4 MB")

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
class ProviderState:
    error: str | None = None
    closed: bool = False
    cancelled: bool = False
    committed_text: str = ""
    current_segment: str = ""
    latest_text: str = ""
    final_received: bool = False
    final_emitted: bool = False
    upstream_error: bool = False
    session_started: bool = False


@dataclass(frozen=True)
class StreamingConfig:
    identity_path: Path
    finish_grace_secs: float
    no_trailing_punctuation: bool
    timeout: float
    endpoint: str = "wss://frontier-audio-ime-ws.doubao.com/ocean/api/v1/ws"
    libopus_path: str = ""
    codec: str = "speech_opus"
    opus_bitrate: int = 32000


def load_config() -> StreamingConfig:
    identity_path = Path(os.getenv("VINPUT_ASR_CREDENTIAL_PATH") or DEFAULT_IDENTITY)
    finish_grace_str = os.getenv("VINPUT_ASR_FINISH_GRACE_SECS", "9.0").strip()
    try:
        finish_grace_secs = float(finish_grace_str) if finish_grace_str else 9.0
    except ValueError as exc:
        raise ConfigError("err_runtime", detail="VINPUT_ASR_FINISH_GRACE_SECS must be a number.") from exc

    no_trailing = (os.getenv("VINPUT_ASR_NO_TRAILING_PUNCTUATION") or "").lower() in {"1", "true", "yes"}
    timeout_str = os.getenv("VINPUT_ASR_TIMEOUT", "10.0").strip()
    try:
        timeout = float(timeout_str) if timeout_str else 10.0
    except ValueError as exc:
        raise ConfigError("err_runtime", detail="VINPUT_ASR_TIMEOUT must be a number.") from exc

    endpoint = os.getenv("VINPUT_ASR_ENDPOINT", "wss://frontier-audio-ime-ws.doubao.com/ocean/api/v1/ws").strip()
    libopus_path = os.getenv("VINPUT_ASR_LIBOPUS_PATH", "").strip()

    if not 0 < finish_grace_secs <= 9.0:
        raise ConfigError("err_runtime", detail="VINPUT_ASR_FINISH_GRACE_SECS must be between 0 and 9 seconds.")
    if timeout <= 0:
        raise ConfigError("err_runtime", detail="VINPUT_ASR_TIMEOUT must be positive.")

    return StreamingConfig(
        identity_path=identity_path,
        finish_grace_secs=finish_grace_secs,
        no_trailing_punctuation=no_trailing,
        timeout=timeout,
        endpoint=endpoint,
        libopus_path=libopus_path,
    )


# Global runtime singletons
_CONFIG: StreamingConfig | None = None
_CLIENT: WebSocketClient | None = None
_ENCODER: OpusEncoder | None = None
_STATE: ProviderState = ProviderState()
_STOP_EVENT = threading.Event()
_FINAL_EVENT = threading.Event()
_TASK_ID: str = ""
_SEQUENCE: int = 0
_PCM_BUFFER = bytearray()
_DEVICE_ID: str = ""
_INSTALL_ID: str = ""


def connect(config: StreamingConfig) -> None:
    global \
        _CONFIG, \
        _CLIENT, \
        _ENCODER, \
        _STATE, \
        _STOP_EVENT, \
        _FINAL_EVENT, \
        _TASK_ID, \
        _SEQUENCE, \
        _PCM_BUFFER, \
        _DEVICE_ID, \
        _INSTALL_ID
    _CONFIG = config
    _STATE = ProviderState()
    _STOP_EVENT.clear()
    _FINAL_EVENT.clear()
    _SEQUENCE = 0
    _PCM_BUFFER.clear()

    identity = ensure_identity(config.identity_path)
    _DEVICE_ID = str(identity["device_id"])
    _INSTALL_ID = str(identity.get("install_id", ""))

    _TASK_ID = str(uuid.uuid4())
    query = {"device_id": _DEVICE_ID, "aid": "401734"}
    if _INSTALL_ID:
        query["iid"] = _INSTALL_ID
    separator = "&" if "?" in config.endpoint else "?"
    url = config.endpoint + separator + urlencode(query)
    headers = {"proto-version": "v2", "sdk-version": "2", "x-custom-keepalive": "true"}

    _CLIENT = WebSocketClient(url, headers, config.timeout)
    try:
        _ENCODER = OpusEncoder(config.opus_bitrate, config.libopus_path)
    except Exception as exc:
        notify_error("err_opus", detail=str(exc))
        raise

    _send_request("StartTask")
    _send_request(
        "StartSession",
        payload={
            "audio_info": {"sample_rate": 16000, "channel": 1, "format": config.codec},
            "extra": {
                "enable_punctuation": True,
                "enable_asr_twopass": True,
                "enable_asr_threepass": True,
                "end_smooth_window_ms": 800,
                "did": _DEVICE_ID,
                "app_name": "oime",
                "app_version": "1.5.1",
            },
        },
    )
    emit_session_started(_TASK_ID)
    _STATE.session_started = True


def _send_request(
    event: str,
    *,
    payload: dict[str, Any] | None = None,
    audio: bytes = b"",
    sequence: int = 0,
) -> None:
    if not _CLIENT:
        return
    request = WebSocketRequest(
        namespace="ASR",
        event=event,
        task_id=_TASK_ID,
        appkey=APP_KEY if event != "TaskRequest" else "",
        payload=json.dumps(payload, ensure_ascii=False, separators=(",", ":")) if payload is not None else "",
        audio=audio,
        seq_id=sequence,
    )
    _CLIENT.send_binary(request.serialize_to_string())


def on_audio_chunk(pcm_bytes: bytes, is_commit: bool) -> None:
    del is_commit
    global _SEQUENCE
    if not _ENCODER or not _CLIENT or _STATE.cancelled:
        return
    _PCM_BUFFER.extend(pcm_bytes)
    while len(_PCM_BUFFER) >= 320 and not _STATE.cancelled:
        frame_pcm = bytes(_PCM_BUFFER[:320])
        del _PCM_BUFFER[:320]
        opus_frame = _ENCODER.encode(frame_pcm)
        _SEQUENCE += 1
        _send_request(
            "TaskRequest",
            audio=opus_frame,
            sequence=_SEQUENCE,
            payload={"extra": {}, "timestamp_ms": int(time.time() * 1000)},
        )


def on_finish() -> None:
    global _SEQUENCE
    if not _ENCODER or not _CLIENT or _STATE.cancelled:
        return
    if _PCM_BUFFER:
        frame_pcm = bytes(_PCM_BUFFER).ljust(320, b"\0")
        _PCM_BUFFER.clear()
        opus_frame = _ENCODER.encode(frame_pcm)
        _SEQUENCE += 1
        _send_request(
            "TaskRequest",
            audio=opus_frame,
            sequence=_SEQUENCE,
            payload={"extra": {}, "timestamp_ms": int(time.time() * 1000)},
        )

    _send_request("FinishSession")


def on_cancel() -> None:
    _STATE.cancelled = True
    cleanup()


def _handle_payload(payload_str: str) -> None:
    try:
        body = json.loads(payload_str)
    except Exception:
        return

    results = body.get("results")
    if not isinstance(results, list) or not results or not isinstance(results[0], dict):
        return

    first_result = results[0]
    text = str(first_result.get("text", "")).strip()
    is_vad_finished = bool(first_result.get("is_vad_finished"))

    if not text:
        return

    total_text = _STATE.committed_text + text
    if _CONFIG and _CONFIG.no_trailing_punctuation:
        formatted = remove_trailing_punctuation(total_text)
    else:
        formatted = total_text

    if formatted and formatted != _STATE.latest_text:
        _STATE.latest_text = formatted
        emit_partial(formatted)

    if is_vad_finished:
        _STATE.committed_text += text


def on_upstream_message(raw_msg: str | bytes) -> None:
    if isinstance(raw_msg, str):
        raw_msg = raw_msg.encode("utf-8")

    try:
        response = WebSocketResponse.from_string(raw_msg)
    except Exception:
        return

    if response.task_id and response.task_id != _TASK_ID:
        return
    if response.event == "Ping":
        _send_request("Pong")
        return
    if response.event == "Pong":
        return

    if response.event in {"SessionFailed", "TaskFailed"}:
        code = str(response.status_code)
        detail = f"{response.event}: {response.status_code} {response.status_text}"
        notify_error("err_upstream", code=code, detail=detail)
        _STATE.upstream_error = True
        _STATE.final_received = True
        _FINAL_EVENT.set()
        return

    if response.payload:
        _handle_payload(response.payload)

    if response.event in {"SessionFinished", "TaskFinished"}:
        _STATE.final_received = True
        _FINAL_EVENT.set()
        if response.event != "TaskFinished":
            _send_request("FinishTask")


def cleanup() -> None:
    global _CLIENT, _ENCODER
    _STOP_EVENT.set()
    _FINAL_EVENT.set()
    if _CLIENT:
        _CLIENT.close()
        _CLIENT = None
    if _ENCODER:
        _ENCODER.close()
        _ENCODER = None


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
                    if not _STATE.final_received and not _STOP_EVENT.is_set():
                        _STATE.upstream_error = True
                        notify_error("err_network", detail="Doubao IME connection closed unexpectedly.")
                        _FINAL_EVENT.set()
                    break
                opcode, payload = message
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

        if not _STATE.cancelled and _STATE.latest_text and not _STATE.final_emitted:
            emit_final(_STATE.latest_text)
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
