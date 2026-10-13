#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import base64
import ctypes
import ctypes.util
import fcntl
import gzip
import hashlib
import json
import os
import secrets
import signal
import socket
import ssl
import struct
import sys
import tempfile
import threading
import time
import unicodedata
import uuid
from collections.abc import AsyncIterable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _gf_mul(left, right):
    result = 0
    while right:
        if right & 1:
            result ^= left
        left = ((left << 1) ^ (0x11B if left & 0x80 else 0)) & 0xFF
        right >>= 1
    return result


def _gf_pow(value, exponent):
    result = 1
    while exponent:
        if exponent & 1:
            result = _gf_mul(result, value)
        value = _gf_mul(value, value)
        exponent >>= 1
    return result


def _sbox(value):
    inverse = 0 if value == 0 else _gf_pow(value, 254)
    result = inverse ^ ((inverse << 1) | (inverse >> 7)) ^ ((inverse << 2) | (inverse >> 6))
    result ^= ((inverse << 3) | (inverse >> 5)) ^ ((inverse << 4) | (inverse >> 4)) ^ 0x63
    return result & 0xFF


def _rot_word(word):
    return word[1:] + word[:1]


def _expand_key(key):
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


def _aes_block(block, key):
    rounds = len(key) // 4 + 6
    round_keys = _expand_key(key)
    state = list(block)

    def add_round_key(round_key):
        for index in range(16):
            state[index] ^= round_key[index]

    def substitute():
        for index in range(16):
            state[index] = _sbox(state[index])

    def shift_rows():
        original = state[:]
        for row in range(4):
            for column in range(4):
                state[4 * column + row] = original[4 * ((column + row) % 4) + row]

    def mix_columns():
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


def _aes_cbc_encrypt(data, key, iv):
    if len(iv) != 16 or len(data) % 16:
        raise ValueError("AES-CBC requires a 16-byte IV and aligned data")
    result = bytearray()
    previous = iv
    for offset in range(0, len(data), 16):
        block = bytes(left ^ right for left, right in zip(data[offset : offset + 16], previous, strict=True))
        previous = _aes_block(block, key)
        result.extend(previous)
    return bytes(result)


def encode_tt_encrypt(gzip_bytes, salt=None):
    salt = secrets.token_bytes(32) if salt is None else salt
    if len(salt) != 32:
        raise ValueError("ttEncrypt salt must contain exactly 32 bytes")
    constant = bytes.fromhex(
        "4dd4c2e6b83162090e52b3c7a6733ba41cb2462b829ab58a196b39db57177524f49baf7f08e8d68d26a72e37c1a95a2f1f05a51892aef2949732b62a38aadd58"
    )
    digest = hashlib.sha512(hashlib.sha512(salt).digest() + constant).digest()
    key, iv = digest[:16], digest[16:32]
    payload = hashlib.sha512(gzip_bytes).digest() + gzip_bytes
    padding = 16 - len(payload) % 16
    payload += bytes([padding]) * padding
    return bytes.fromhex("746305100000") + salt + _aes_cbc_encrypt(payload, key, iv)


def new_installation() -> dict:
    return {
        "openudid": secrets.token_hex(10),
        "clientudid": str(uuid.uuid4()),
        "cdid": str(uuid.uuid4()),
        "first_install_time": int(time.time() * 1000),
    }


def registration_request(state: dict) -> dict:
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


def register_device(request: dict) -> tuple[dict, dict]:
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


DEFAULT_IDENTITY = Path.home() / ".cache/vinput/doubaoime/credentials.json"
LEGACY_IDENTITIES = (
    Path.home() / ".cache/vinput/doubaoime-stable/identity.json",
    Path.home() / ".cache/vinput/doubaoime-asr/credentials.json",
)
IDENTITY_FIELDS = ("device_id", "install_id", "cdid", "openudid", "clientudid")


def save_identity(path: Path, state: dict):
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


def migrated_identity():
    for source in LEGACY_IDENTITIES:
        try:
            state = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(state, dict) or not all(state.get(key) and str(state[key]) != "0" for key in IDENTITY_FIELDS):
            continue
        result: dict = {key: str(state[key]) for key in IDENTITY_FIELDS}
        installed = state.get("first_install_time")
        if isinstance(installed, int) and installed > 0:
            result["first_install_time"] = installed
        return result
    return None


def _ensure_identity(path: Path) -> dict:
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
            state = migrated_identity() if path == DEFAULT_IDENTITY else None
            state = state if state is not None else new_installation()
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
        # Save the installation before registering so retries keep the same identity.
        save_identity(path, state)
        identifiers, _ = register_device(registration_request(state))
        state.update(identifiers)
        save_identity(path, state)
        return state


async def ensure_identity(path=DEFAULT_IDENTITY):
    return await background(_ensure_identity, path)


PRESERVED = frozenset("]})>\u300b\u201d'\"\u3011\u2019\uff09%\u2103:\uff1a")


def remove_trailing_punctuation(text, *, per_line=False):
    def trim(value):
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


class OpusEncoder:
    def __init__(self, bitrate: int):
        library = os.getenv("VINPUT_ASR_LIBOPUS_PATH", "").strip() or ctypes.util.find_library("opus")
        if library is None:
            raise RuntimeError("libopus is required for speech_opus")
        self.lib = ctypes.CDLL(library)
        self.lib.opus_encoder_create.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
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
            # Constant bitrate, one 10 ms packet per request.
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

    def close(self):
        if self.handle:
            self.lib.opus_encoder_destroy(self.handle)
            self.handle = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
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
    fields: dict[int, Any] = {}
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


def get_proto_string(fields: dict[int, Any], field_number: int) -> str:
    value = fields.get(field_number, b"")
    if isinstance(value, list):
        value = value[-1]
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, int):
        return str(value)
    return ""


def get_proto_int(fields: dict[int, Any], field_number: int) -> int:
    value = fields.get(field_number, 0)
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

    def SerializeToString(self):
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
    def FromString(cls, data):
        fields = parse_protobuf_fields(data)
        return cls(
            get_proto_string(fields, 1),
            get_proto_string(fields, 4),
            get_proto_int(fields, 5),
            get_proto_string(fields, 6),
            get_proto_string(fields, 7),
            get_proto_int(fields, 9),
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
            log_id = headers.get("x-tt-logid")
            if log_id:
                details.append(f"log_id={log_id}")
            connect_id = headers.get("x-api-connect-id")
            if connect_id:
                details.append(f"connect_id={connect_id}")
            if " 403" in f" {status_line} ":
                details.append("Doubao IME rejected the anonymous WebSocket handshake")
            raise RuntimeError(", ".join(details))

        accept = headers.get("sec-websocket-accept")
        expected = base64.b64encode(hashlib.sha1((key + GUID).encode("utf-8")).digest()).decode("ascii")
        if accept != expected:
            raise RuntimeError("WebSocket handshake failed: invalid Sec-WebSocket-Accept header.")
        self.response_headers = headers

    def close(self):
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


async def background(function, *args):
    loop = asyncio.get_running_loop()
    future = loop.create_future()

    def complete(value=None, error=None):
        if not future.done():
            if error is None:
                future.set_result(value)
            else:
                future.set_exception(error)

    def work():
        try:
            value = function(*args)
        except BaseException as error:
            try:
                loop.call_soon_threadsafe(complete, None, error)
            except RuntimeError:
                pass
        else:
            try:
                loop.call_soon_threadsafe(complete, value)
            except RuntimeError:
                pass

    threading.Thread(target=work, daemon=True).start()
    return await future


@asynccontextmanager
async def connect_ws(url, headers, timeout):
    client = None
    abandoned = threading.Event()

    def open_connection():
        nonlocal client
        client = WebSocketClient(url, headers, timeout)
        client.socket.settimeout(None)
        if abandoned.is_set():
            client.close()
            raise ConnectionError("Connection cancelled")
        return client

    class Connection:
        def __init__(self, connected: WebSocketClient):
            self.client = connected

        async def send(self, data):
            await background(self.client.send_binary, data)

        async def recv(self):
            message = await background(self.client.recv_message)
            if message is None:
                raise ConnectionError("WebSocket closed before final result")
            opcode, data = message
            if opcode != 2:
                raise ValueError("Expected a binary WebSocket message")
            return data

    try:
        connected = await background(open_connection)
        yield Connection(connected)
    finally:
        abandoned.set()
        if client is not None:
            client.close()


APP_KEY = "OrnqKvSSrs"


class ASRError(RuntimeError):
    def __init__(self, response: WebSocketResponse):
        self.code = response.status_code
        self.event = response.event
        super().__init__(f"{response.event}: {response.status_code} {response.status_text}")


@dataclass(frozen=True)
class ASRConfig:
    device_id: str
    install_id: str = ""
    endpoint: str = "wss://frontier-audio-ime-ws.doubao.com/ocean/api/v1/ws"
    appkey: str | None = field(default=None, repr=False)
    codec: str = "speech_opus"
    opus_bitrate: int = 32000
    chunk_ms: int = 10
    open_timeout: float = 4.0
    response_timeout: float = 10.0
    retries: int = 2
    headers: dict[str, str] = field(default_factory=dict)
    extra: dict = field(default_factory=dict)

    def __post_init__(self):
        if not self.device_id or self.device_id == "0":
            raise ValueError("A nonzero device_id is required")
        if self.codec not in {"pcm", "speech_opus"}:
            raise ValueError("codec must be pcm or speech_opus")
        if self.chunk_ms <= 0 or self.chunk_ms % 10:
            raise ValueError("chunk_ms must be a positive multiple of 10")
        if self.codec == "speech_opus" and self.chunk_ms != 10:
            raise ValueError("speech_opus requires one 10 ms Opus packet per WebSocket request")
        if self.retries < 0:
            raise ValueError("retries must be nonnegative")


@dataclass(frozen=True)
class ASRUpdate:
    text: str
    vad_finished: bool
    stream_finished: bool
    sequence: int
    payload: dict


@dataclass(frozen=True)
class ASRResult:
    text: str
    task_id: str
    updates: tuple[ASRUpdate, ...]


class TranscriptState:
    """Append completed VAD segments; replace hypotheses within one segment."""

    def __init__(self):
        self.committed = ""
        self.text = ""
        self.current = ""
        self.finished = False

    def apply(self, text: str, finished: bool) -> str | None:
        # Drop empty interim hypotheses and duplicate segment finals.
        if (not text and not finished) or (finished and self.finished and text == self.current):
            return None
        self.text = self.committed + text
        self.current = text
        self.finished = finished
        if finished:
            self.committed = self.text
        return self.text


def decode_response(data: bytes | str) -> WebSocketResponse:
    if not isinstance(data, bytes):
        raise ValueError("ASR server returned text instead of a binary Protobuf message")
    try:
        response = WebSocketResponse.FromString(data)
    except ValueError as error:
        raise ValueError("Invalid ASR Protobuf response") from error
    if response.event in {"SessionFailed", "TaskFailed"}:
        raise ASRError(response)
    return response


def parse_update(response: WebSocketResponse) -> ASRUpdate | None:
    """Decode recognition fields independently of the WebSocket lifecycle."""
    if not response.payload:
        return None
    body = json.loads(response.payload)
    if not isinstance(body, dict):
        raise ValueError("ASR payload must be a JSON object")
    results = body.get("results", [])
    if results is None:
        return None
    if not isinstance(results, list):
        raise ValueError("ASR results must be an array")
    if not results or not isinstance(results[0], dict) or not isinstance(results[0].get("text"), str):
        return None
    extra = body.get("extra")
    if extra is None:
        extra = {}
    if not isinstance(extra, dict):
        raise ValueError("ASR recognition extra must be an object")
    result = results[0]
    return ASRUpdate(
        result["text"],
        bool(result.get("is_vad_finished")),
        bool(result.get("stream_asr_finish")),
        extra.get("seq_id", response.seq_id),
        body,
    )


class ASRClient:
    def __init__(self, config: ASRConfig):
        self.config = config
        key = config.appkey
        if key is None:
            key = APP_KEY
        self._appkey = key
        if not self._appkey:
            raise ValueError("ASR application key is empty")

    async def transcribe(self, pcm: bytes, on_update: Callable[[ASRUpdate], None] | None = None) -> ASRResult:
        """Transcribe buffered audio; callback updates restart after a retry.

        Only the successful attempt's updates are included in the returned result.
        """
        if len(pcm) % 2:
            raise ValueError("PCM bytes must contain complete 16-bit samples")
        if not pcm:
            raise ValueError("Audio is empty")
        for attempt in range(self.config.retries + 1):
            try:
                return await self._transcribe(pcm, on_update)
            except (OSError, TimeoutError, ConnectionError):
                if attempt == self.config.retries:
                    raise
                # A buffered file can restart from the beginning after a transport
                # failure. Each attempt owns a fresh task, avoiding stale results.
                await asyncio.sleep(min(0.2 * 2**attempt, 1.0))
        raise AssertionError("Unreachable retry state")

    async def transcribe_stream(
        self,
        source: AsyncIterable[bytes],
        on_update: Callable[[ASRUpdate], None] | None = None,
        *,
        on_start: Callable[[], None] | None = None,
    ) -> ASRResult:
        """Consume live 16 kHz mono PCM; report updates before the source ends.

        The producer owns pacing. A live source cannot be replayed after a
        disconnect, so transport and source errors propagate without retry.
        """
        return await self._transcribe_stream(source, on_update, on_start)

    async def _transcribe(self, pcm: bytes, on_update: Callable[[ASRUpdate], None] | None) -> ASRResult:
        async def paced_audio():
            chunk_size = self.config.chunk_ms * 32
            began = asyncio.get_running_loop().time()
            for offset in range(0, len(pcm), chunk_size):
                yield pcm[offset : offset + chunk_size]
                target = began + min(offset + chunk_size, len(pcm)) / 32000
                await asyncio.sleep(max(0, target - asyncio.get_running_loop().time()))

        return await self._transcribe_stream(paced_audio(), on_update)

    async def _transcribe_stream(
        self,
        source: AsyncIterable[bytes],
        on_update: Callable[[ASRUpdate], None] | None,
        on_start: Callable[[], None] | None = None,
    ) -> ASRResult:
        config = self.config
        task_id = str(uuid.uuid4())
        query = {"device_id": config.device_id, "aid": "401734"}
        if config.install_id:
            query["iid"] = config.install_id
        separator = "&" if "?" in config.endpoint else "?"
        url = config.endpoint + separator + urlencode(query)
        headers = {"proto-version": "v2", "sdk-version": "2", "x-custom-keepalive": "true", **config.headers}
        async with connect_ws(url, headers, config.open_timeout) as ws:

            async def send(event: str, *, payload: dict | None = None, audio: bytes = b"", sequence: int = 0):
                request = WebSocketRequest(
                    namespace="ASR",
                    event=event,
                    task_id=task_id,
                    appkey=self._appkey if event != "TaskRequest" else "",
                    payload=json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
                    if payload is not None
                    else "",
                    audio=audio,
                    seq_id=sequence,
                )
                await ws.send(request.SerializeToString())

            async def receive() -> WebSocketResponse:
                while True:
                    response = decode_response(await ws.recv())
                    if response.task_id and response.task_id != task_id:
                        continue
                    if response.event == "Ping":
                        await send("Pong")
                        continue
                    if response.event == "Pong":
                        continue
                    return response

            async def wait_for(event: str):
                deadline = asyncio.get_running_loop().time() + config.response_timeout
                while True:
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        raise TimeoutError(f"Timed out waiting for {event}")
                    response = await asyncio.wait_for(receive(), remaining)
                    if response.event == event:
                        return response

            await send("StartTask")
            await wait_for("TaskStarted")
            extra = {
                "enable_punctuation": True,
                "enable_asr_twopass": True,
                "enable_asr_threepass": True,
                "end_smooth_window_ms": 800,
                "did": config.device_id,
                "app_name": "oime",
                "app_version": "1.5.1",
                **config.extra,
            }
            await send(
                "StartSession",
                payload={
                    "audio_info": {"sample_rate": 16000, "channel": 1, "format": config.codec},
                    "extra": extra,
                },
            )
            await wait_for("SessionStarted")
            if on_start is not None:
                on_start()

            audio_finished = False

            async def send_audio():
                nonlocal audio_finished
                chunk_size = config.chunk_ms * 32
                iterator = source.__aiter__()
                encoder = None
                pending = bytearray()
                sequence = 0

                async def send_frame(frame):
                    nonlocal sequence
                    if encoder is not None:
                        frame = encoder.encode(frame.ljust(chunk_size, b"\0"))
                    sequence += 1
                    await send(
                        "TaskRequest",
                        audio=frame,
                        sequence=sequence,
                        payload={"extra": {}, "timestamp_ms": int(time.time() * 1000)},
                    )

                try:
                    if config.codec == "speech_opus":
                        encoder = OpusEncoder(config.opus_bitrate)
                    async for chunk in iterator:
                        if not isinstance(chunk, bytes):
                            raise TypeError("Live PCM chunks must be bytes")
                        if len(chunk) % 2:
                            raise ValueError("PCM bytes must contain complete 16-bit samples")
                        pending.extend(chunk)
                        while len(pending) >= chunk_size:
                            frame = bytes(pending[:chunk_size])
                            del pending[:chunk_size]
                            await send_frame(frame)
                    if pending:
                        await send_frame(bytes(pending))
                    if not sequence:
                        raise ValueError("Audio is empty")
                    # A fast server can reply before send() returns. Mark EOF
                    # before sending FinishSession; awaiting sender still checks errors.
                    audio_finished = True
                    await send("FinishSession")
                finally:
                    if encoder is not None:
                        encoder.close()
                    close = getattr(iterator, "aclose", None)
                    if close is not None:
                        await close()

            sender = asyncio.create_task(send_audio())
            updates: list[ASRUpdate] = []
            transcript = TranscriptState()
            incoming = asyncio.create_task(receive())
            finish_deadline = None
            try:
                while True:
                    if sender.done():
                        await sender
                        if finish_deadline is None:
                            finish_deadline = asyncio.get_running_loop().time() + config.response_timeout
                        remaining = finish_deadline - asyncio.get_running_loop().time()
                        if remaining <= 0:
                            raise TimeoutError("ASR session did not finish before its deadline")
                        response = await asyncio.wait_for(incoming, remaining)
                    else:
                        completed, _ = await asyncio.wait({incoming, sender}, return_when=asyncio.FIRST_COMPLETED)
                        if incoming not in completed:
                            continue
                        response = await incoming
                    update = parse_update(response)
                    if update is not None:
                        text = transcript.apply(update.text, update.vad_finished)
                        if text is not None:
                            update = replace(update, text=text)
                            updates.append(update)
                            if on_update:
                                on_update(update)
                    if response.event in {"SessionFinished", "TaskFinished"}:
                        break
                    incoming = asyncio.create_task(receive())
                if not audio_finished:
                    raise RuntimeError("ASR session finished before the audio source ended")
                await sender
                if response.event != "TaskFinished":
                    await send("FinishTask")
                return ASRResult(transcript.text, task_id, tuple(updates))
            finally:
                for task in (sender, incoming):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(sender, incoming, return_exceptions=True)


MAX_BUFFER_BYTES = 320000


class AudioInput:
    def __init__(self):
        self.queue = asyncio.Queue(maxsize=1001)
        self.buffered = 0
        self.finish_deadline = None
        self.cancelled = False
        self.changed = asyncio.Event()

    def finish(self, grace):
        if self.finish_deadline is None:
            self.finish_deadline = asyncio.get_running_loop().time() + grace
            self.queue.put_nowait(None)
            self.changed.set()

    async def read(self, lines, grace):
        async for line in lines:
            if not line.strip():
                continue
            message = json.loads(line)
            if not isinstance(message, dict):
                raise ValueError("Provider input must be a JSON object")
            kind = message.get("type")
            if kind == "cancel":
                self.cancelled = True
                self.changed.set()
                return
            if kind == "finish":
                self.finish(grace)
            elif kind == "audio":
                if self.finish_deadline is not None:
                    raise ValueError("Audio received after finish")
                audio = base64.b64decode(message["audio_base64"], validate=True)
                if len(audio) % 2:
                    raise ValueError("PCM must contain complete 16-bit samples")
                if audio:
                    if self.buffered + len(audio) > MAX_BUFFER_BYTES or self.queue.qsize() >= 1000:
                        raise ValueError("Audio buffer exceeded 10 seconds while the provider was unavailable")
                    self.buffered += len(audio)
                    self.queue.put_nowait(audio)
                if message.get("commit") is True:
                    self.finish(grace)
            elif kind != "finish":
                raise ValueError(f"Unsupported provider input: {kind}")
        self.finish(grace)

    async def chunks(self):
        while True:
            item = await self.queue.get()
            if item is None:
                return
            self.buffered -= len(item)
            yield item


async def run_provider(lines, emit, *, identity_path=DEFAULT_IDENTITY, finish_grace=9.0, no_trailing_punctuation=False):
    if not 0 < finish_grace <= 9:
        raise ValueError("Finish grace must be greater than zero and at most 9 seconds")
    audio = AudioInput()

    def partial(update):
        if update.text:
            text = remove_trailing_punctuation(update.text) if no_trailing_punctuation else update.text
            emit({"type": "partial", "text": text})

    async def recognize():
        state = await ensure_identity(identity_path)
        client = ASRClient(
            ASRConfig(str(state["device_id"]), str(state["install_id"]), response_timeout=finish_grace, retries=0)
        )
        return await client.transcribe_stream(
            audio.chunks(), partial, on_start=lambda: emit({"type": "session_started", "session_id": str(uuid.uuid4())})
        )

    reader = asyncio.create_task(audio.read(lines, finish_grace))
    recognition = asyncio.create_task(recognize())
    changed = asyncio.create_task(audio.changed.wait())
    try:
        while True:
            tasks: set[asyncio.Task[Any]] = {recognition}
            if not reader.done():
                tasks.add(reader)
            if not changed.done():
                tasks.add(changed)
            timeout = (
                None if audio.finish_deadline is None else audio.finish_deadline - asyncio.get_running_loop().time()
            )
            if timeout is not None and timeout <= 0:
                raise TimeoutError("Timed out waiting for the final transcript")
            done, _ = await asyncio.wait(tasks, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
            if not done:
                raise TimeoutError("Timed out waiting for the final transcript")
            if reader.done():
                await reader
            if audio.cancelled:
                return 0
            if recognition in done:
                result = await recognition
                text = remove_trailing_punctuation(result.text) if no_trailing_punctuation else result.text
                if not text:
                    raise ValueError("No speech was recognized")
                emit({"type": "final", "text": text})
                return 0
    except asyncio.CancelledError:
        raise
    except Exception as error:
        emit({"type": "error", "message": f"{type(error).__name__}: {error}"})
        return 1
    finally:
        for task in (reader, recognition, changed):
            task.cancel()
        await asyncio.gather(reader, recognition, changed, return_exceptions=True)
        emit({"type": "closed"})


async def stdin_lines():
    reader = asyncio.StreamReader(limit=1_000_000)
    protocol = asyncio.StreamReaderProtocol(reader)
    transport, _ = await asyncio.get_running_loop().connect_read_pipe(lambda: protocol, sys.stdin.buffer)
    try:
        while line := await reader.readline():
            yield line
    finally:
        transport.close()


def main():
    parser = argparse.ArgumentParser(description="Doubao IME streaming provider")
    parser.add_argument(
        "--identity", type=Path, default=Path(os.getenv("VINPUT_ASR_CREDENTIAL_PATH") or DEFAULT_IDENTITY)
    )
    parser.add_argument("--finish-grace", type=float, default=float(os.getenv("VINPUT_ASR_FINISH_GRACE_SECS") or "9"))
    parser.add_argument(
        "--no-trailing-punctuation",
        action="store_true",
        default=(os.getenv("VINPUT_ASR_NO_TRAILING_PUNCTUATION") or "").lower() in {"1", "true", "yes"},
        help="Remove final punctuation locally, preserving punctuation within the text",
    )
    args = parser.parse_args()
    if not 0 < args.finish_grace <= 9:
        parser.error("finish grace must be greater than zero and at most 9 seconds")

    async def run():
        task = asyncio.current_task()
        assert task is not None
        for sig in (signal.SIGTERM, signal.SIGINT):
            asyncio.get_running_loop().add_signal_handler(sig, task.cancel)
        return await run_provider(
            stdin_lines(),
            lambda event: print(json.dumps(event, ensure_ascii=False), flush=True),
            identity_path=args.identity,
            finish_grace=args.finish_grace,
            no_trailing_punctuation=args.no_trailing_punctuation,
        )

    try:
        raise SystemExit(asyncio.run(run()))
    except (KeyboardInterrupt, asyncio.CancelledError):
        raise SystemExit(130) from None


if __name__ == "__main__":
    main()
