#!/usr/bin/env python3
"""WeType IME streaming ASR: standard-library JSONL bridge using system Opus/OpenSSL."""

from __future__ import annotations

import base64
import ctypes
import ctypes.util
import hashlib
import json
import math
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
from typing import Any
from urllib.parse import urlparse

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
OPUS_APPLICATION_VOIP = 2048
OPUS_MAX_PACKET_SIZE = 4000
CONNECT_PROBE_SECONDS = 0.25

EXIT_SUCCESS = 0
EXIT_RUNTIME_ERROR = 1
EXIT_USAGE_ERROR = 2

MESSAGES: dict[str, dict[str, str]] = {
    "en_US": {
        "title": "WeType Streaming ASR",
        "err_invalid_event": "Invalid input event: {detail}",
        "err_opus": "WeType streaming: failed to load or initialize libopus: {detail}",
        "err_network": "WeType streaming network error: {detail}",
        "err_upstream": "WeType streaming server error: {detail}",
        "err_runtime": "WeType streaming runtime error: {detail}",
    },
    "zh_CN": {
        "title": "微信输入法实时语音识别",
        "err_invalid_event": "输入事件格式错误：{detail}",
        "err_opus": "微信输入法流式语音识别：加载或初始化 libopus 失败：{detail}",
        "err_network": "微信输入法流式语音识别网络连接失败：{detail}",
        "err_upstream": "微信输入法流式语音服务端报错：{detail}",
        "err_runtime": "微信输入法流式语音运行异常：{detail}",
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


def get_optional_env(name: str, default: str = "") -> str:
    return os.environ.get(name, "").strip() or default


def positive_seconds(name: str, default: float) -> float:
    try:
        value = float(get_optional_env(name, str(default)))
    except ValueError as exc:
        raise ConfigError("err_runtime", detail=f"{name} must be a positive finite number") from exc
    if not math.isfinite(value) or value <= 0:
        raise ConfigError("err_runtime", detail=f"{name} must be a positive finite number")
    return value


@dataclass
class ProviderState:
    error: str | None = None
    closed: bool = False
    cancelled: bool = False
    latest_text: str = ""
    final_received: bool = False
    final_emitted: bool = False
    upstream_error: bool = False
    session_started: bool = False
    polished_text: str = ""


@dataclass(frozen=True)
class StreamingConfig:
    timeout: float
    finish_grace_secs: float
    libopus_path: str
    credential_path: str


def load_config() -> StreamingConfig:
    timeout = positive_seconds("VINPUT_ASR_TIMEOUT", 15)
    finish_grace_secs = positive_seconds("VINPUT_ASR_FINISH_GRACE_SECS", 5)
    libopus_path = get_optional_env("VINPUT_ASR_LIBOPUS_PATH")
    credential_path = get_optional_env("VINPUT_ASR_CREDENTIAL_PATH", "~/.cache/vinput/wetype/identity.json")

    return StreamingConfig(
        timeout=timeout,
        finish_grace_secs=finish_grace_secs,
        libopus_path=libopus_path,
        credential_path=credential_path,
    )


class OpusEncoder:
    def __init__(self, sample_rate: int, channels: int, custom_path: str = "") -> None:
        self.lib = None
        if custom_path:
            try:
                self.lib = ctypes.CDLL(custom_path)
            except OSError as exc:
                raise RuntimeError(f"Failed to load libopus from VINPUT_ASR_LIBOPUS_PATH={custom_path}: {exc}") from exc
        else:
            candidates: list[str] = []
            discovered = ctypes.util.find_library("opus")
            if discovered:
                candidates.append(discovered)
            candidates.extend(["libopus.so.0", "libopus.so"])
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
            OPUS_APPLICATION_VOIP,
            ctypes.byref(error),
        )
        if not self.encoder or error.value != 0:
            raise RuntimeError(f"libopus encoder init failed: {error.value}")

        self.lib.opus_encoder_ctl.restype = ctypes.c_int
        for request, value in ((4002, 64000), (4010, 9)):
            result = self.lib.opus_encoder_ctl(
                ctypes.c_void_p(self.encoder),
                ctypes.c_int(request),
                ctypes.c_int(value),
            )
            if result != 0:
                raise RuntimeError(f"libopus encoder configuration failed: {result}")

    def encode(self, pcm_frame: bytes, samples_per_frame: int) -> bytes:
        if self.lib is None or self.encoder is None:
            raise RuntimeError("libopus encoder is not initialized.")
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

    def destroy(self) -> None:
        encoder = getattr(self, "encoder", None)
        lib = getattr(self, "lib", None)
        if encoder and lib:
            try:
                lib.opus_encoder_destroy(encoder)
            except Exception:
                pass
            self.encoder = None

    def __del__(self) -> None:
        self.destroy()


class WebSocketClient:
    def __init__(self, url: str, headers: dict[str, str], timeout: float) -> None:
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
        self.socket = self._connect()

    def _connect(self) -> socket.socket:
        raw_sock = self._dial()
        raw_sock.settimeout(self.timeout)

        if self.scheme == "wss":
            context = ssl.create_default_context()
            try:
                sock = context.wrap_socket(raw_sock, server_hostname=self.host)
            except Exception:
                raw_sock.close()
                raise
        else:
            sock = raw_sock

        key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
        host_header = self.host
        if not ((self.scheme == "wss" and self.port == 443) or (self.scheme == "ws" and self.port == 80)):
            host_header = f"{self.host}:{self.port}"
        lines = [
            f"GET {self.path} HTTP/1.1",
            f"Host: {host_header}",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {key}",
            "Sec-WebSocket-Version: 13",
        ]
        for name, value in self.headers.items():
            lines.append(f"{name}: {value}")
        request = "\r\n".join(lines) + "\r\n\r\n"
        try:
            sock.sendall(request.encode("utf-8"))
            response = self._read_http_response(sock)
            self._validate_handshake(response, key)
        except Exception:
            sock.close()
            raise
        return sock

    def _dial(self) -> socket.socket:
        addrs = socket.getaddrinfo(self.host, self.port, socket.AF_UNSPEC, socket.SOCK_STREAM)
        last: OSError | None = None
        for probe in (True, False):
            for family, kind, proto, _canon, sockaddr in addrs:
                sock = socket.socket(family, kind, proto)
                sock.settimeout(min(CONNECT_PROBE_SECONDS, self.timeout) if probe else self.timeout)
                try:
                    sock.connect(sockaddr)
                    return sock
                except OSError as exc:
                    last = exc
                    sock.close()
        raise last or OSError(f"cannot connect to {self.host}:{self.port}")

    def _read_http_response(self, sock: socket.socket) -> bytes:
        data = bytearray()
        while b"\r\n\r\n" not in data:
            chunk = sock.recv(4096)
            if not chunk:
                raise RuntimeError("WebSocket handshake failed: empty response.")
            data.extend(chunk)
            if len(data) > 65536:
                raise RuntimeError("WebSocket handshake failed: response too large.")
        self._recv_buffer = bytes(data).split(b"\r\n\r\n", 1)[1]
        return bytes(data)

    def _validate_handshake(self, response: bytes, key: str) -> None:
        header_blob = response.split(b"\r\n\r\n", 1)[0].decode("utf-8", errors="replace")
        lines = header_blob.split("\r\n")
        if not lines or len(lines[0].split()) < 2 or lines[0].split()[1] != "101":
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
        try:
            if not self._closed:
                self._send_frame(0x8, b"")
        except OSError:
            pass
        try:
            self.socket.close()
        finally:
            self._closed = True

    def abort(self) -> None:
        self._closed = True
        try:
            self.socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.socket.close()

    def send_binary(self, payload: bytes) -> None:
        self._send_frame(0x2, payload)

    def recv_binary(self) -> bytes | None:
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
            if opcode not in {0x0, 0x2}:
                continue

            if opcode == 0x2:
                current_opcode = opcode
                fragments = bytearray(payload)
            else:
                if current_opcode is None:
                    continue
                fragments.extend(payload)

            if len(fragments) > 8 * 1024 * 1024:
                raise ValueError("WebSocket message too large")
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

        if length > 8 * 1024 * 1024:
            raise ValueError("WebSocket frame too large")
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


# Wire protocol reference: J3n5en/voicekey, crates/core/src/wetype.rs.
HOST = "wetype.weixin.qq.com"
SIGN_KEY = "zN7rB3bL4pO8jW1o"
BOOT_KEY = b"D4Y5U3Y2M0C0T7N4P1P7O2N6E1I2Y1U6"
CMD_DH, CMD_UIN = 2147483646, 0x7FFFFDFD
P = 0xFFFFFFFDFFFFFFFFFFFFFFFFFFFFFFFF
N = 0xFFFFFFFE0000000075A30D1B9038A115
G = (0x161FF7528B899B2D0C28607CA52C5B86, 0xCF5AC8395BAFEB13C02DA292DDED7A83)
B = 0xE87579C11079F43DD824993C2CEE5ED3


def point_add(a: tuple[int, int] | None, b: tuple[int, int] | None) -> tuple[int, int] | None:
    if a is None:
        return b
    if b is None:
        return a
    x, y = a
    u, v = b
    if x == u and (y + v) % P == 0:
        return None
    slope = ((3 * x * x - 3) * pow(2 * y, -1, P) if a == b else (v - y) * pow(u - x, -1, P)) % P
    z = (slope * slope - x - u) % P
    return z, (slope * (x - z) - y) % P


def point_mul(k: int, point: tuple[int, int] | None = G) -> tuple[int, int] | None:
    out: tuple[int, int] | None = None
    while k:
        if k & 1:
            out = point_add(out, point)
        point = point_add(point, point)
        k >>= 1
    return out


def shared_key(k: int, public: str) -> bytes:
    if len(public) != 66 or not public.startswith("04"):
        raise ValueError("Invalid server public key")
    x, y = int(public[2:34], 16), int(public[34:], 16)
    if not (0 <= x < P and 0 <= y < P) or (y * y - x * x * x + 3 * x - B) % P:
        raise ValueError("Server public key is not on curve")
    point = point_mul(k, (x, y))
    if point is None:
        raise ValueError("Invalid shared point")
    return f"{point[0]:032X}".encode()


class AES:
    def __init__(self) -> None:
        self.lib = ctypes.CDLL(ctypes.util.find_library("crypto") or "libcrypto.so.3")
        specs = {
            "EVP_CIPHER_CTX_new": ([], ctypes.c_void_p),
            "EVP_CIPHER_CTX_free": ([ctypes.c_void_p], None),
            "EVP_aes_256_ecb": ([], ctypes.c_void_p),
            "EVP_CipherInit_ex": (
                [
                    ctypes.c_void_p,
                    ctypes.c_void_p,
                    ctypes.c_void_p,
                    ctypes.c_char_p,
                    ctypes.c_void_p,
                    ctypes.c_int,
                ],
                ctypes.c_int,
            ),
            "EVP_CipherUpdate": (
                [
                    ctypes.c_void_p,
                    ctypes.c_void_p,
                    ctypes.POINTER(ctypes.c_int),
                    ctypes.c_char_p,
                    ctypes.c_int,
                ],
                ctypes.c_int,
            ),
            "EVP_CipherFinal_ex": (
                [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)],
                ctypes.c_int,
            ),
        }
        for name, (args, result) in specs.items():
            fn = getattr(self.lib, name)
            fn.argtypes = args
            fn.restype = result

    def crypt(self, key: bytes, data: bytes, encrypt: bool = True) -> bytes:
        if key is None or len(key) != 32:
            raise ValueError("AES key must be 32 bytes")
        c = self.lib.EVP_CIPHER_CTX_new()
        if not c:
            raise RuntimeError("AES allocation failed")
        try:
            out = ctypes.create_string_buffer(len(data) + 32)
            n = ctypes.c_int()
            end = ctypes.c_int()
            if self.lib.EVP_CipherInit_ex(c, self.lib.EVP_aes_256_ecb(), None, key, None, int(encrypt)) != 1:
                raise ValueError("AES init failed")
            if self.lib.EVP_CipherUpdate(c, out, ctypes.byref(n), data, len(data)) != 1:
                raise ValueError("AES update failed")
            if self.lib.EVP_CipherFinal_ex(c, ctypes.byref(out, n.value), ctypes.byref(end)) != 1:
                raise ValueError("AES padding invalid")
            return out.raw[: n.value + end.value]
        finally:
            self.lib.EVP_CIPHER_CTX_free(c)


def varint(n: int) -> bytes:
    out = bytearray()
    while n > 127:
        out.append((n & 127) | 128)
        n >>= 7
    out.append(n)
    return bytes(out)


def read_varint(data: bytes, i: int) -> tuple[int, int]:
    n = 0
    for shift in range(0, 70, 7):
        if i >= len(data):
            raise ValueError("Truncated varint")
        b = data[i]
        i += 1
        n |= (b & 127) << shift
        if b < 128:
            return n, i
    raise ValueError("Invalid varint")


def field(n: int, value: str | bytes | int) -> bytes:
    if isinstance(value, str):
        value = value.encode()
    if isinstance(value, bytes):
        return varint(n * 8 + 2) + varint(len(value)) + value
    return varint(n * 8) + varint(value)


def fields(data: bytes) -> list[tuple[int, Any]]:
    out: list[tuple[int, Any]] = []
    i = 0
    while i < len(data):
        tag, i = read_varint(data, i)
        wire = tag & 7
        if wire == 0:
            value, i = read_varint(data, i)
        elif wire in (1, 2, 5):
            if wire == 2:
                size, i = read_varint(data, i)
            else:
                size = 8 if wire == 1 else 4
            if i + size > len(data):
                raise ValueError("Truncated protobuf")
            value = data[i : i + size]
            i += size
        else:
            raise ValueError("Unsupported protobuf wire type")
        out.append((tag >> 3, value))
    return out


def compress(data: bytes) -> bytes:
    return varint(len(data)) + b"".join(
        bytes([(len(data[i : i + 60]) - 1) * 4]) + data[i : i + 60] for i in range(0, len(data), 60)
    )


def decompress(data: bytes) -> bytes:
    size, i = read_varint(data, 0)
    out = bytearray()
    if size > 8 * 1024 * 1024:
        raise ValueError("Snappy message too large")

    def take(n: int) -> bytes:
        nonlocal i
        if i + n > len(data):
            raise ValueError("Truncated snappy")
        chunk = data[i : i + n]
        i += n
        return chunk

    while i < len(data):
        tag = take(1)[0]
        mode = tag & 3
        if mode == 0:
            n = tag >> 2
            if n >= 60:
                n = int.from_bytes(take(n - 59), "little")
            out.extend(take(n + 1))
        else:
            if mode == 1:
                offset = ((tag >> 5) << 8) | take(1)[0]
                n = ((tag >> 2) & 7) + 4
            else:
                offset = int.from_bytes(take(2 if mode == 2 else 4), "little")
                n = (tag >> 2) + 1
            if not 0 < offset <= len(out) or len(out) + n > size:
                raise ValueError("Invalid snappy copy")
            for _ in range(n):
                out.append(out[-offset])
        if len(out) > size:
            raise ValueError("Snappy length exceeded")
    if len(out) != size:
        raise ValueError("Snappy length mismatch")
    return bytes(out)


def random_lower(n: int) -> str:
    return "".join(secrets.choice("abcdefghijklmnopqrstuvwxyz") for _ in range(n))


class Client:
    def __init__(self, timeout: float) -> None:
        self.aes = AES()
        self.ws = WebSocketClient("wss://" + HOST + "/", {"Sec-WebSocket-Protocol": "wxws_pb"}, timeout)
        self.key: bytes | None = None
        self.task = 0
        self.uin = "0"
        self.device = ""
        self.time_offset_ms = 0

    def roundtrip(
        self,
        path: str,
        body: bytes = b"",
        cmd: int = 0,
        compression: str = "1",
        token: str = "",
    ) -> tuple[bytes, dict[str, str]]:
        special = cmd in (CMD_DH, CMD_UIN)
        if not special:
            self.task += 1
        task = cmd if special else self.task
        ts = str(self._now_ms())
        trace = random_lower(16)
        digest = hashlib.md5(body).hexdigest().upper()
        values = ["5", "2.2.3(657)", "2"]
        if cmd == CMD_DH:
            values += [ts, digest, trace, str(cmd)]
        elif cmd == CMD_UIN:
            values += [ts, digest, token, trace, str(cmd)]
        else:
            values += [str(cmd), "0", ts, digest, self.uin, trace, str(task)]
        headers: dict[str, str] = {"Kb-Uin": self.uin}
        if token:
            headers["Kb-GenUinToken"] = token
        if special:
            headers["Kb-DeviceCodeRestrictionV2"] = "1"
        if cmd != CMD_DH:
            if self.key:
                headers["Kb-SharedKeySuffix"] = self.key[-4:].decode()
            headers.update({"Kb-CmdId": str(cmd), "Kb-SubCmdId": "0"})
        headers.update(
            {
                "Kb-OsType": "5",
                "Kb-Version": "2.2.3(657)",
                "Kb-SystemVersion": "27.0.0",
                "Kb-PackageType": "3",
                "Use_DebugNet": "0",
                "Kb-TimeStamp": ts,
                "Kb-BodyMd5": digest,
                "Kb-TraceId": trace,
                "Kb-TaskId": str(task),
                "Kb-Scene": "2",
                "Kb-Sign": hashlib.sha256(("".join(values) + SIGN_KEY).encode()).hexdigest().upper(),
                "Content-Length": str(len(body)),
                "Kb-CompressionType": compression,
                "Content-Type": "application/octet-stream",
                "HOST": HOST,
            }
        )
        http = field(1, "POST") + field(3, path) + field(4, "")
        http += b"".join(field(5, field(1, k) + field(2, v)) for k, v in headers.items()) + field(6, body)
        self.ws.send_binary(field(1, 0) + field(2, 0) + field(3, task) + field(5, http))
        raw = self.ws.recv_binary()
        if raw is None:
            raise RuntimeError("WeType closed connection")
        response = fields(dict(fields(raw)).get(4, b""))
        parsed = dict(response)
        if parsed.get(2) != 200:
            raise RuntimeError(f"WeType HTTP status {parsed.get(2, 0)} at {path}")
        resp_headers: dict[str, str] = {}
        for n, value in response:
            if n == 5 or not isinstance(value, bytes):
                continue
            try:
                kv = dict(fields(value))
                resp_headers[kv[1].decode()] = kv[2].decode()
            except (ValueError, KeyError, AttributeError, UnicodeError):
                pass
        return parsed.get(5, b""), resp_headers

    def exchange(self, register: bool) -> tuple[str, str]:
        k = secrets.randbelow(N - 1) + 1
        point = point_mul(k)
        if point is None:
            raise ValueError("Invalid local ECDH key")
        x, y = point
        public = f"04{x:032X}{y:032X}"
        req = (
            field(1, public)
            + field(2, public)
            + field(4, self.device)
            + field(7, "")
            + field(8, "Mac16,12")
            + field(9, "")
        )
        req += field(3, 1) if register else field(5, int(self.uin))
        body, _ = self.roundtrip("/oauth_pubkey_v2", self.aes.crypt(BOOT_KEY, req), CMD_DH)
        reply = dict(fields(self.aes.crypt(BOOT_KEY, body, False)))
        server = reply[2].decode()
        self.key = shared_key(k, server)
        return server, reply.get(4, b"").decode()

    def _now_ms(self) -> int:
        return time.time_ns() // 1000000 + self.time_offset_ms

    def _sync_time(self) -> None:
        body, _ = self.roundtrip("/timestamp")
        try:
            server = dict(fields(body)).get(1)
        except ValueError:
            return
        if isinstance(server, int) and 10**12 <= server <= 10**13:
            self.time_offset_ms = server - time.time_ns() // 1000000

    def handshake(self, credential_path: str) -> None:
        self._sync_time()
        path = Path(credential_path).expanduser()
        cached = False
        try:
            identity = json.loads(path.read_text())
            if not isinstance(identity, dict) or not isinstance(identity.get("device"), str):
                raise ValueError("Invalid cached device identity")
            if not identity["device"] or int(identity.get("uin", 0)) <= 0:
                raise ValueError("Invalid cached device identity")
            self.device = identity["device"]
            self.uin = str(identity["uin"])
            self.exchange(False)
            cached = True
        except (OSError, ValueError, TypeError, KeyError, RuntimeError):
            pass
        if not cached:
            self.uin = "0"
            body = "MAC" + "0" * (17 - len("Mac16,12")) + "Mac16,12" + random_lower(12)
            self.device = body + hashlib.md5((body + SIGN_KEY).encode()).hexdigest().upper()
            server, token = self.exchange(True)
            if not token:
                raise RuntimeError("WeType registration token missing")
            req = field(1, self.device) + field(2, server) + field(4, token)
            assert self.key is not None
            body, _ = self.roundtrip("/gen_uin_v2", self.aes.crypt(self.key, req), CMD_UIN, token=token)
            self.uin = str(dict(fields(self.aes.crypt(self.key, body, False))).get(2, 0))
            if self.uin == "0":
                raise RuntimeError("WeType registration failed")
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + "." + secrets.token_hex(6))
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump({"device": self.device, "uin": self.uin}, f)
                os.replace(tmp, path)
            finally:
                tmp.unlink(missing_ok=True)
        assert self.key is not None
        self.roundtrip("/api_v2", self.aes.crypt(self.key, b""), 8074)

    def voice(
        self,
        voice_id: str,
        opus: bytes | None,
        seq: int,
        total: int,
        end: bool,
    ) -> tuple[str, str]:
        req = field(2, voice_id) + field(5, 5) + field(7, seq) + field(22, 1) + field(23, 1) + field(24, 1)
        if opus:
            req += field(4, opus)
        if end:
            req += field(6, 1)
        if total:
            req += field(11, total)
        assert self.key is not None
        body, headers = self.roundtrip("/api_v2", self.aes.crypt(self.key, compress(field(1, req))), 4548, "2")
        plain = self.aes.crypt(self.key, body, False)
        if headers.get("Kb-CompressionType", headers.get("CompressionType")) == "2":
            plain = decompress(plain)
        reply = dict(fields(plain))
        if 1 in reply and isinstance(reply[1], bytes):
            reply = dict(fields(reply[1]))
        return reply.get(4, b"").decode(), reply.get(14, b"").decode()


# Global runtime singletons
_CONFIG: StreamingConfig | None = None
_CLIENT: Client | None = None
_ENCODER: OpusEncoder | None = None
_STATE: ProviderState = ProviderState()
_STOP_EVENT = threading.Event()
_FINAL_EVENT = threading.Event()
_VOICE_ID: str = ""
_SEQ: int = 0
_TOTAL: int = 0
_PACKETS: list[bytes] = []
_PCM_BUFFER = bytearray()


def connect(config: StreamingConfig) -> None:
    global _CONFIG, _CLIENT, _ENCODER, _STATE, _STOP_EVENT, _FINAL_EVENT, _VOICE_ID, _SEQ, _TOTAL, _PACKETS, _PCM_BUFFER
    _CONFIG = config
    _STATE = ProviderState()
    _STOP_EVENT.clear()
    _FINAL_EVENT.clear()
    _SEQ = 0
    _TOTAL = 0
    _PACKETS.clear()
    _PCM_BUFFER.clear()

    _CLIENT = Client(config.timeout)
    _CLIENT.handshake(config.credential_path)
    try:
        _ENCODER = OpusEncoder(16000, 1, config.libopus_path)
    except Exception as exc:
        notify_error("err_opus", detail=str(exc))
        raise

    _VOICE_ID = uuid.uuid4().hex
    emit_session_started(_VOICE_ID)
    _STATE.session_started = True


def _send_voice(end: bool = False, poll: bool = False) -> None:
    global _SEQ, _TOTAL
    if not _CLIENT or _STATE.cancelled:
        return
    if poll:
        framed = None
        number = 0
    else:
        _SEQ += 1
        number = _SEQ
        framed = (b"#!OPUS_RAW_V1" + bytes([2, 1, 0])) if _SEQ == 1 else b""
        framed += b"".join(struct.pack("<H", len(p)) + p for p in _PACKETS)
        _PACKETS.clear()
        _TOTAL += len(framed)

    raw, final = _CLIENT.voice(_VOICE_ID, framed, number, _TOTAL, end)
    on_upstream_message(json.dumps({"raw": raw, "final": final}))


def on_audio_chunk(pcm_bytes: bytes, is_commit: bool) -> None:
    del is_commit
    if not _ENCODER or not _CLIENT or _STATE.cancelled:
        return
    _PCM_BUFFER.extend(pcm_bytes)
    while len(_PCM_BUFFER) >= 640 and not _STATE.cancelled:
        _PACKETS.append(_ENCODER.encode(bytes(_PCM_BUFFER[:640]), 320))
        del _PCM_BUFFER[:640]
        if len(_PACKETS) == 6:
            _send_voice()


def on_finish() -> None:
    if not _ENCODER or not _CLIENT or _STATE.cancelled:
        return
    if _PCM_BUFFER:
        _PACKETS.append(_ENCODER.encode(bytes(_PCM_BUFFER).ljust(640, b"\0"), 320))
        _PCM_BUFFER.clear()

    if not _STATE.latest_text:
        _STATE.final_received = True
        _FINAL_EVENT.set()
        return

    assert _CONFIG is not None
    deadline = time.monotonic() + _CONFIG.finish_grace_secs
    try:
        _CLIENT.ws.socket.settimeout(min(_CONFIG.timeout, _CONFIG.finish_grace_secs))
        _send_voice(end=True)
        while not _STATE.polished_text:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or _STOP_EVENT.wait(min(0.03, remaining)):
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            _CLIENT.ws.socket.settimeout(min(_CONFIG.timeout, remaining))
            _send_voice(end=True, poll=True)
    except TimeoutError:
        pass
    _STATE.final_received = True
    _FINAL_EVENT.set()


def on_cancel() -> None:
    _STATE.cancelled = True
    cleanup()


def on_upstream_message(raw_msg: str | bytes) -> None:
    if isinstance(raw_msg, bytes):
        raw_msg = raw_msg.decode("utf-8", errors="replace")
    try:
        data = json.loads(raw_msg)
    except Exception:
        return

    raw = str(data.get("raw", "")).strip()
    final = str(data.get("final", "")).strip()
    if raw and raw != _STATE.latest_text:
        _STATE.latest_text = raw
        emit_partial(raw)
    if final:
        _STATE.polished_text = final
        _STATE.latest_text = final


def cleanup() -> None:
    global _CLIENT, _ENCODER
    _STOP_EVENT.set()
    _FINAL_EVENT.set()
    if _CLIENT:
        _CLIENT.ws.abort()
        _CLIENT = None
    if _ENCODER:
        _ENCODER.destroy()
        _ENCODER = None


def run() -> int:
    config = load_config()

    try:
        connect(config)
    except Exception as exc:
        notify_error("err_network", detail=str(exc))
        return EXIT_RUNTIME_ERROR

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
            emit_final(_STATE.polished_text or _STATE.latest_text)
            _STATE.final_emitted = True

        cleanup()

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
