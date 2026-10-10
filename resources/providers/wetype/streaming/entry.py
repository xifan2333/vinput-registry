#!/usr/bin/env python3
"""WeType IME streaming ASR: standard-library JSONL bridge using system Opus/OpenSSL."""

import base64
import ctypes
import ctypes.util
import hashlib
import json
import math
import os
import queue
import secrets
import socket
import ssl
import struct
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
OPUS_APPLICATION_AUDIO = 2049
OPUS_MAX_PACKET_SIZE = 4000


def get_optional_env(name, default=""):
    return os.environ.get(name, "").strip() or default


class OpusEncoder:
    def __init__(self, sample_rate: int, channels: int) -> None:
        self.lib = None
        env_path = get_optional_env("VINPUT_ASR_LIBOPUS_PATH")
        if env_path:
            try:
                self.lib = ctypes.CDLL(env_path)
            except OSError as exc:
                raise RuntimeError(f"Failed to load libopus from VINPUT_ASR_LIBOPUS_PATH={env_path}: {exc}") from exc
        else:
            candidates = []
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
            OPUS_APPLICATION_AUDIO,
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

    def __del__(self) -> None:
        encoder = getattr(self, "encoder", None)
        lib = getattr(self, "lib", None)
        if encoder and lib:
            try:
                lib.opus_encoder_destroy(encoder)
            except Exception:
                pass


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
        raw_sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
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
    """The protocol uses the uppercase shared X hex as the 32-byte AES key."""
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
    """Use the system OpenSSL EVP API for AES-256-ECB with PKCS#7 padding."""

    def __init__(self):
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

    def crypt(self, key, data, encrypt=True):
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


def varint(n):
    out = bytearray()
    while n > 127:
        out.append((n & 127) | 128)
        n >>= 7
    out.append(n)
    return bytes(out)


def read_varint(data, i):
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


def field(n, value):
    if isinstance(value, str):
        value = value.encode()
    if isinstance(value, bytes):
        return varint(n * 8 + 2) + varint(len(value)) + value
    return varint(n * 8) + varint(value)


def fields(data: bytes) -> list[tuple[int, Any]]:
    out = []
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


def compress(data):
    return varint(len(data)) + b"".join(
        bytes([(len(data[i : i + 60]) - 1) * 4]) + data[i : i + 60] for i in range(0, len(data), 60)
    )


def decompress(data):
    size, i = read_varint(data, 0)
    out = bytearray()
    if size > 8 * 1024 * 1024:
        raise ValueError("Snappy message too large")

    def take(n):
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


def random_lower(n):
    return "".join(secrets.choice("abcdefghijklmnopqrstuvwxyz") for _ in range(n))


class Client:
    def __init__(self):
        self.aes = AES()
        self.ws = WebSocketClient(
            "wss://" + HOST + "/", {"Sec-WebSocket-Protocol": "wxws_pb"}, positive_seconds("VINPUT_ASR_TIMEOUT", 15)
        )
        self.key: bytes | None = None
        self.task = 0
        self.uin = "0"
        self.device = ""

    def roundtrip(self, path, body=b"", cmd=0, compression="1", token=""):
        special = cmd in (CMD_DH, CMD_UIN)
        if not special:
            self.task += 1
        task = cmd if special else self.task
        ts = str(time.time_ns() // 1000000)
        trace = random_lower(16)
        digest = hashlib.md5(body).hexdigest().upper()
        values = ["5", "2.2.3(657)", "2"]
        if cmd == CMD_DH:
            values += [ts, digest, trace, str(cmd)]
        elif cmd == CMD_UIN:
            values += [ts, digest, token, trace, str(cmd)]
        else:
            values += [str(cmd), "0", ts, digest, self.uin, trace, str(task)]
        headers = {"Kb-Uin": self.uin}
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
        headers = {}
        for n, value in response:
            if n == 5 or not isinstance(value, bytes):
                continue
            try:
                kv = dict(fields(value))
                headers[kv[1].decode()] = kv[2].decode()
            except (ValueError, KeyError, AttributeError, UnicodeError):
                pass
        return parsed.get(5, b""), headers

    def exchange(self, register):
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

    def handshake(self):
        self.roundtrip("/timestamp")
        path = Path(get_optional_env("VINPUT_ASR_CREDENTIAL_PATH", "~/.cache/vinput/wetype/identity.json")).expanduser()
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
        self.roundtrip("/api_v2", self.aes.crypt(self.key, b""), 8074)

    def voice(self, voice_id, opus, seq, total, end):
        req = field(2, voice_id) + field(5, 5) + field(7, seq) + field(22, 1) + field(23, 1) + field(24, 1)
        if opus:
            req += field(4, opus)
        if end:
            req += field(6, 1)
        if total:
            req += field(11, total)
        body, headers = self.roundtrip("/api_v2", self.aes.crypt(self.key, compress(field(1, req))), 4548, "2")
        plain = self.aes.crypt(self.key, body, False)
        if headers.get("Kb-CompressionType", headers.get("CompressionType")) == "2":
            plain = decompress(plain)
        reply = dict(fields(plain))
        if 1 in reply and isinstance(reply[1], bytes):
            reply = dict(fields(reply[1]))
        return reply.get(4, b"").decode(), reply.get(14, b"").decode()


def positive_seconds(name: str, default: float) -> float:
    value = float(get_optional_env(name, str(default)))
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a positive finite number")
    return value


def emit(kind: str, **values: Any) -> None:
    print(json.dumps({"type": kind, **values}, ensure_ascii=False), flush=True)


def run() -> int:
    timeout = positive_seconds("VINPUT_ASR_TIMEOUT", 15)
    grace = positive_seconds("VINPUT_ASR_FINISH_GRACE_SECS", 5)
    inbox: queue.Queue[dict[str, Any]] = queue.Queue()
    cancelled = threading.Event()
    done = threading.Event()
    active: list[Client] = []
    errors: list[Exception] = []

    def reader() -> None:
        try:
            for line in sys.stdin:
                if not line.strip():
                    continue
                event = json.loads(line)
                if not isinstance(event, dict):
                    raise ValueError("Expected a JSON object")
                kind = event.get("type")
                if kind == "cancel":
                    cancelled.set()
                    return
                if kind not in ("audio", "finish"):
                    raise ValueError(f"Unknown event type: {kind}")
                inbox.put(event)
                if kind == "finish":
                    return
            # EOF without finish is an abort, never a request to submit text.
            cancelled.set()
        except Exception as exc:
            inbox.put({"type": "error", "error": str(exc)})

    def worker() -> None:
        client = None
        try:
            if cancelled.is_set():
                return
            client = Client()
            active.append(client)
            if cancelled.is_set():
                return
            client.handshake()
            if cancelled.is_set():
                return
            encoder = OpusEncoder(16000, 1)
            voice_id = uuid.uuid4().hex
            emit("session_started", session_id=voice_id, config={})
            seq = total = 0
            text = polished = ""
            packets: list[bytes] = []
            pcm = bytearray()

            def send(end: bool = False, poll: bool = False) -> None:
                nonlocal seq, total, text, polished
                if cancelled.is_set():
                    return
                if poll:
                    framed = None
                    number = 0
                else:
                    seq += 1
                    number = seq
                    framed = (b"#!OPUS_RAW_V1" + bytes([2, 1, 0])) if seq == 1 else b""
                    framed += b"".join(struct.pack("<H", len(packet)) + packet for packet in packets)
                    packets.clear()
                    total += len(framed)
                assert client is not None
                raw, final = client.voice(voice_id, framed, number, total, end)
                if cancelled.is_set():
                    return
                if raw and raw != text:
                    text = raw
                    emit("partial", text=text)
                # Do not mistake an intermediate server revision for the utterance final.
                if end and final:
                    polished = final

            while not cancelled.is_set():
                try:
                    event = inbox.get(timeout=0.1)
                except queue.Empty:
                    continue
                kind = event["type"]
                if kind == "audio":
                    pcm.extend(base64.b64decode(event["audio_base64"], validate=True))
                    while len(pcm) >= 640 and not cancelled.is_set():
                        packets.append(encoder.encode(bytes(pcm[:640]), 320))
                        del pcm[:640]
                        if len(packets) == 6:
                            send()
                elif kind == "finish":
                    if pcm:
                        if len(pcm) % 2:
                            raise ValueError("PCM must contain 16-bit samples")
                        packets.append(encoder.encode(bytes(pcm).ljust(640, b"\0"), 320))
                    if seq == 0 and not packets:
                        emit("final", text="", segment_final=True, utterance_final=True)
                        return
                    deadline = time.monotonic() + grace
                    try:
                        client.ws.socket.settimeout(min(timeout, grace))
                        send(True)
                        for _ in range(8):
                            remaining = deadline - time.monotonic()
                            if polished or remaining <= 0 or cancelled.wait(min(0.25, remaining)):
                                break
                            remaining = deadline - time.monotonic()
                            if remaining <= 0:
                                break
                            client.ws.socket.settimeout(min(timeout, remaining))
                            send(True, True)
                    except TimeoutError:
                        if not (text or polished):
                            raise
                        # After finish only, retain the latest transcript on a finalization timeout.
                    if not cancelled.is_set():
                        emit("final", text=polished or text, segment_final=True, utterance_final=True)
                    return
                elif kind == "error":
                    raise ValueError(event["error"])
        except Exception as exc:
            if not cancelled.is_set():
                errors.append(exc)
        finally:
            if client is not None:
                client.ws.abort()
            done.set()

    threading.Thread(target=reader, daemon=True).start()
    threading.Thread(target=worker, daemon=True).start()
    while not done.wait(0.02):
        if cancelled.is_set():
            # Exit promptly even if DNS/TLS or a server response is still blocked.
            if active:
                active[0].ws.abort()
            emit("closed")
            return 0
    if errors:
        raise errors[0]
    emit("closed")
    return 0


def main() -> int:
    try:
        return run()
    except Exception as exc:
        message = f"WeType ASR: {exc}"
        emit("error", message=message)
        print(message, file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
