#!/usr/bin/env python3
"""iFlytek (XFyun) duplex streaming speech-to-text provider via WebSocket protocol."""

from __future__ import annotations

import base64
import gzip
import hashlib
import hmac
import json
import os
import secrets
import socket
import ssl
import struct
import sys
import threading
import uuid
from dataclasses import dataclass, field
from email.utils import formatdate
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

DEFAULT_URL = "wss://iat.cn-huabei-1.xf-yun.com/v1"
DEFAULT_TIMEOUT = 30
DEFAULT_FINISH_GRACE_SECS = 8.0
MAX_FINISH_GRACE_SECS = 8.0
MAX_WEBSOCKET_MESSAGE_BYTES = 4 * 1024 * 1024
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

EXIT_SUCCESS = 0
EXIT_RUNTIME_ERROR = 1
EXIT_USAGE_ERROR = 2

MESSAGES: dict[str, dict[str, str]] = {
    "en_US": {
        "title": "iFlytek Streaming ASR",
        "err_missing_app_id": "Missing VINPUT_ASR_APP_ID.",
        "err_missing_api_key": "Missing VINPUT_ASR_API_KEY.",
        "err_missing_api_secret": "Missing VINPUT_ASR_API_SECRET.",
        "err_invalid_event": "Invalid input event: {detail}",
        "err_network": "iFlytek streaming network error: {detail}",
        "err_upstream": "iFlytek streaming server error {code}: {detail}",
        "err_runtime": "iFlytek streaming runtime error: {detail}",
    },
    "zh_CN": {
        "title": "讯飞开放平台实时语音识别",
        "err_missing_app_id": "缺少 VINPUT_ASR_APP_ID 环境变量。",
        "err_missing_api_key": "缺少 VINPUT_ASR_API_KEY 环境变量。",
        "err_missing_api_secret": "缺少 VINPUT_ASR_API_SECRET 环境变量。",
        "err_invalid_event": "输入事件格式错误：{detail}",
        "err_network": "讯飞流式语音识别网络连接失败：{detail}",
        "err_upstream": "讯飞流式语音服务端报错 {code}：{detail}",
        "err_runtime": "讯飞流式语音运行异常：{detail}",
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


def normalize_text(text: str) -> str:
    return text.strip()


def is_ascii_word_character(value: str) -> bool:
    return bool(value) and value.isascii() and (value.isalnum() or value == "_")


def combine_text_fragments(fragments: list[str]) -> str:
    combined = ""
    for fragment in fragments:
        frag = normalize_text(fragment)
        if not frag:
            continue
        if combined and is_ascii_word_character(combined[-1]) and is_ascii_word_character(frag[0]):
            combined += " "
        combined += frag
    return combined


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
    segments: dict[int, str] = field(default_factory=dict)


@dataclass(frozen=True)
class StreamingConfig:
    app_id: str
    api_key: str
    api_secret: str
    base_url: str
    res_id: str
    timeout: int
    finish_grace_secs: float
    enable_wpgs: bool
    bos: int | None
    vgap: int | None
    eos: int | None
    vinfo: int | None
    evl: int | None
    opt: int | None
    ltc: int | None
    ett: int | None
    svad: int
    fa_nbest: bool | None
    res_language: str
    hotwords: str
    context: str


def require_env(name: str, key: str = "err_missing_api_key") -> str:
    val = os.getenv(name, "").strip()
    if not val:
        raise ConfigError(key)
    return val


def optional_env(name: str, default: str = "") -> str:
    val = os.getenv(name, "").strip()
    return val if val else default


def optional_int_env(name: str, default: int) -> int:
    val = os.getenv(name, "").strip()
    if not val:
        return default
    try:
        return int(val)
    except ValueError as exc:
        raise ConfigError("err_runtime", detail=f"{name} must be an integer.") from exc


def optional_float_env(name: str, default: float) -> float:
    val = os.getenv(name, "").strip()
    if not val:
        return default
    try:
        return float(val)
    except ValueError as exc:
        raise ConfigError("err_runtime", detail=f"{name} must be a number.") from exc


def optional_bool_env(name: str, default: bool) -> bool:
    val = os.getenv(name, "").strip().lower()
    if not val:
        return default
    if val in {"1", "true", "yes", "on"}:
        return True
    if val in {"0", "false", "no", "off"}:
        return False
    raise ConfigError("err_runtime", detail=f"{name} must be a boolean.")


def optional_nullable_int_env(name: str) -> int | None:
    val = os.getenv(name, "").strip()
    if not val:
        return None
    try:
        return int(val)
    except ValueError as exc:
        raise ConfigError("err_runtime", detail=f"{name} must be an integer.") from exc


def optional_nullable_bool_env(name: str) -> bool | None:
    val = os.getenv(name, "").strip().lower()
    if not val:
        return None
    if val in {"1", "true", "yes", "on"}:
        return True
    if val in {"0", "false", "no", "off"}:
        return False
    raise ConfigError("err_runtime", detail=f"{name} must be a boolean.")


def load_config() -> StreamingConfig:
    app_id = require_env("VINPUT_ASR_APP_ID", "err_missing_app_id")
    api_key = require_env("VINPUT_ASR_API_KEY", "err_missing_api_key")
    api_secret = require_env("VINPUT_ASR_API_SECRET", "err_missing_api_secret")
    base_url = optional_env("VINPUT_ASR_URL", DEFAULT_URL)
    res_id = optional_env("VINPUT_ASR_RES_ID")
    timeout = optional_int_env("VINPUT_ASR_TIMEOUT", DEFAULT_TIMEOUT)
    finish_grace_secs = optional_float_env("VINPUT_ASR_FINISH_GRACE_SECS", DEFAULT_FINISH_GRACE_SECS)

    if timeout <= 0:
        raise ConfigError("err_runtime", detail="VINPUT_ASR_TIMEOUT must be greater than zero.")
    if finish_grace_secs < 0 or finish_grace_secs > MAX_FINISH_GRACE_SECS:
        raise ConfigError("err_runtime", detail="VINPUT_ASR_FINISH_GRACE_SECS must be between 0 and 8 seconds.")

    svad = optional_int_env("VINPUT_ASR_SVAD", 0)
    if svad not in {0, 1}:
        raise ConfigError("err_runtime", detail="VINPUT_ASR_SVAD must be 0 or 1.")

    return StreamingConfig(
        app_id=app_id,
        api_key=api_key,
        api_secret=api_secret,
        base_url=base_url,
        res_id=res_id,
        timeout=timeout,
        finish_grace_secs=finish_grace_secs,
        enable_wpgs=optional_bool_env("VINPUT_ASR_ENABLE_WPGS", True),
        bos=optional_nullable_int_env("VINPUT_ASR_BOS"),
        vgap=optional_nullable_int_env("VINPUT_ASR_VGAP"),
        eos=optional_nullable_int_env("VINPUT_ASR_EOS"),
        vinfo=optional_nullable_int_env("VINPUT_ASR_VINFO"),
        evl=optional_nullable_int_env("VINPUT_ASR_EVL"),
        opt=optional_nullable_int_env("VINPUT_ASR_OPT"),
        ltc=optional_nullable_int_env("VINPUT_ASR_LTC"),
        ett=optional_nullable_int_env("VINPUT_ASR_ETT"),
        svad=svad,
        fa_nbest=optional_nullable_bool_env("VINPUT_ASR_FA_NBEST"),
        res_language=optional_env("VINPUT_ASR_RES_LANGUAGE"),
        hotwords=optional_env("VINPUT_ASR_HOTWORDS"),
        context=optional_env("VINPUT_ASR_CONTEXT"),
    )


def build_auth_url(url: str, api_key: str, api_secret: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"ws", "wss"} or not parsed.hostname:
        raise ConfigError("err_runtime", detail="VINPUT_ASR_URL must be a ws:// or wss:// URL.")

    port = parsed.port
    default_port = 443 if parsed.scheme == "wss" else 80
    host = parsed.hostname
    if port and port != default_port:
        host = f"{host}:{port}"

    path = parsed.path or "/"
    request_line = f"GET {path} HTTP/1.1"
    date = formatdate(usegmt=True)
    signature_origin = f"host: {host}\ndate: {date}\n{request_line}"
    signature = base64.b64encode(
        hmac.new(api_secret.encode("utf-8"), signature_origin.encode("utf-8"), hashlib.sha256).digest()
    ).decode("ascii")
    authorization_origin = (
        f'api_key="{api_key}", algorithm="hmac-sha256", headers="host date request-line", signature="{signature}"'
    )
    authorization = base64.b64encode(authorization_origin.encode("utf-8")).decode("ascii")

    query = list(parse_qsl(parsed.query, keep_blank_values=True))
    query.extend([("host", host), ("date", date), ("authorization", authorization)])
    return urlunparse(parsed._replace(query=urlencode(query)))


def build_iat_parameters(config: StreamingConfig) -> dict[str, Any]:
    iat: dict[str, Any] = {
        "domain": "spark_asr_v2",
        "language": "zh_cn",
        "accent": "mandarin",
        "result": {
            "encoding": "utf8",
            "compress": "raw",
            "format": "json",
        },
    }
    if config.enable_wpgs:
        iat["dwa"] = "wpgs"

    options = {
        "bos": config.bos,
        "vgap": config.vgap,
        "eos": config.eos,
        "vinfo": config.vinfo,
        "evl": config.evl,
        "opt": config.opt,
        "ltc": config.ltc,
        "ett": config.ett,
    }
    for param_name, param_val in options.items():
        if param_val is not None:
            iat[param_name] = param_val

    iat["svad"] = config.svad
    if config.fa_nbest is not None:
        iat["fa_nbest"] = config.fa_nbest

    if config.res_language:
        iat["rlang"] = config.res_language
    if config.hotwords:
        iat["dhw"] = config.hotwords
    if config.context:
        iat["context"] = config.context

    return iat


def build_audio_message(
    *,
    app_id: str,
    res_id: str,
    status: int,
    sequence: int,
    audio: bytes,
    iat_parameters: dict[str, Any] | None,
) -> dict[str, Any]:
    header: dict[str, Any] = {"app_id": app_id, "status": status}
    if res_id:
        header["res_id"] = res_id

    audio_payload: dict[str, Any] = {
        "encoding": "raw",
        "sample_rate": 16000,
        "channels": 1,
        "bit_depth": 16,
        "seq": sequence,
        "status": status,
        "audio": base64.b64encode(audio).decode("ascii"),
    }
    message: dict[str, Any] = {
        "header": header,
        "payload": {"audio": audio_payload},
    }
    if iat_parameters is not None:
        message["parameter"] = {"iat": iat_parameters}
    return message


class WebSocketClient:
    def __init__(self, url: str, timeout: int) -> None:
        parsed = urlparse(url)
        if parsed.scheme not in {"ws", "wss"} or not parsed.hostname:
            raise VInputError("err_network", detail="Authenticated URL must use ws:// or wss://.")

        self.scheme = parsed.scheme
        self.host = parsed.hostname
        self.port = parsed.port or (443 if parsed.scheme == "wss" else 80)
        self.path = parsed.path or "/"
        if parsed.query:
            self.path += "?" + parsed.query
        self.timeout = timeout
        self._recv_buffer = b""
        self._closed = False
        self._send_lock = threading.Lock()
        self.socket = self._connect()

    def _connect(self) -> socket.socket:
        raw_socket = socket.create_connection((self.host, self.port), timeout=self.timeout)
        raw_socket.settimeout(self.timeout)
        if self.scheme == "wss":
            context = ssl.create_default_context()
            sock = context.wrap_socket(raw_socket, server_hostname=self.host)
        else:
            sock = raw_socket

        key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
        host_header = self.host
        if self.port not in {80, 443}:
            host_header = f"{host_header}:{self.port}"
        request = "\r\n".join(
            [
                f"GET {self.path} HTTP/1.1",
                f"Host: {host_header}",
                "Upgrade: websocket",
                "Connection: Upgrade",
                f"Sec-WebSocket-Key: {key}",
                "Sec-WebSocket-Version: 13",
                "\r\n",
            ]
        )
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
        lines = response.decode("utf-8", errors="replace").split("\r\n")
        if not lines or " 101 " not in f" {lines[0]} ":
            status = lines[0] if lines else "invalid response"
            raise RuntimeError(f"WebSocket handshake failed: {status}")

        headers: dict[str, str] = {}
        for line in lines[1:]:
            if ":" in line:
                name, value = line.split(":", 1)
                headers[name.strip().lower()] = value.strip()
        expected = base64.b64encode(hashlib.sha1((key + GUID).encode("ascii")).digest()).decode("ascii")
        if headers.get("sec-websocket-accept") != expected:
            raise RuntimeError("WebSocket handshake failed: invalid Sec-WebSocket-Accept header.")

    def send_text(self, text: str) -> None:
        self._send_frame(0x1, text.encode("utf-8"))

    def close(self) -> None:
        with self._send_lock:
            if self._closed:
                return
            try:
                self._send_frame_locked(0x8, b"")
            except OSError:
                pass
            try:
                self.socket.close()
            finally:
                self._closed = True

    def recv_message(self) -> tuple[int, bytes] | None:
        fragments = bytearray()
        current_opcode: int | None = None
        fragment_bytes = 0
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
                fragment_bytes = len(payload)
            elif current_opcode is not None:
                fragment_bytes += len(payload)
                if fragment_bytes > MAX_WEBSOCKET_MESSAGE_BYTES:
                    raise RuntimeError("WebSocket message exceeds the 4 MiB limit.")
                fragments.extend(payload)

            if current_opcode is not None and fragment_bytes > MAX_WEBSOCKET_MESSAGE_BYTES:
                raise RuntimeError("WebSocket message exceeds the 4 MiB limit.")

            if not fin:
                continue
            if current_opcode is None:
                continue
            return current_opcode, bytes(fragments)

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        with self._send_lock:
            self._send_frame_locked(opcode, payload)

    def _send_frame_locked(self, opcode: int, payload: bytes) -> None:
        if self._closed:
            return
        mask_key = secrets.token_bytes(4)
        length = len(payload)
        header = bytearray([0x80 | (opcode & 0x0F)])
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", length))
        masked = bytes(value ^ mask_key[index % 4] for index, value in enumerate(payload))
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
        if length > MAX_WEBSOCKET_MESSAGE_BYTES:
            raise RuntimeError("WebSocket frame exceeds the 4 MiB limit.")
        payload = self._recv_exact(length)
        if payload is None:
            return None
        if masked:
            payload = bytes(value ^ mask_key[index % 4] for index, value in enumerate(payload))
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


def extract_result_text(result: dict[str, Any]) -> str:
    parts: list[str] = []
    words = result.get("ws")
    if not isinstance(words, list):
        return ""
    for segment in words:
        if not isinstance(segment, dict):
            continue
        candidates = segment.get("cw")
        if not isinstance(candidates, list):
            continue
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            word = candidate.get("w")
            if isinstance(word, str):
                parts.append(word)
                break
    return combine_text_fragments(parts)


def decoded_result_is_last(result: dict[str, Any] | None) -> bool:
    return bool(result and result.get("ls") is True)


def decode_result_text(text: str, compression: str) -> dict[str, Any]:
    try:
        decoded = base64.b64decode(text, validate=True)
        if compression == "gzip":
            decoded = gzip.decompress(decoded)
        elif compression not in {"raw", ""}:
            raise RuntimeError(f"Unsupported iFlytek result compression: {compression}")
        result = json.loads(decoded.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError, OSError) as exc:
        raise RuntimeError(f"Invalid iFlytek result payload: {exc}") from exc
    if not isinstance(result, dict):
        raise RuntimeError("Invalid iFlytek result payload: expected a JSON object.")
    ret = result.get("ret", 0)
    if ret not in {0, "0", None}:
        raise RuntimeError(f"iFlytek ASR recognition error {ret}: {result.get('msg', 'unknown error')}")
    return result


def update_segments(segments: dict[int, str], result: dict[str, Any]) -> None:
    sequence = result.get("sn")
    if not isinstance(sequence, int):
        sequence = max(segments, default=0) + 1

    pgs = result.get("pgs")
    if pgs == "rpl":
        replacement_range = result.get("rg")
        if isinstance(replacement_range, list) and len(replacement_range) == 2:
            try:
                start, end = int(replacement_range[0]), int(replacement_range[1])
            except (TypeError, ValueError):
                start, end = sequence, sequence
            for index in range(start, end + 1):
                segments.pop(index, None)

    segments[sequence] = extract_result_text(result)


# Global runtime singletons
_CONFIG: StreamingConfig | None = None
_CLIENT: WebSocketClient | None = None
_STATE: ProviderState = ProviderState()
_STOP_EVENT = threading.Event()
_FINAL_EVENT = threading.Event()
_SEQUENCE: int = 1
_SENT_AUDIO: bool = False
_SENT_FINISH: bool = False


def connect(config: StreamingConfig) -> None:
    global _CONFIG, _CLIENT, _STATE, _SEQUENCE, _SENT_AUDIO, _SENT_FINISH
    _CONFIG = config
    _STATE = ProviderState()
    _STOP_EVENT.clear()
    _FINAL_EVENT.clear()
    _SEQUENCE = 1
    _SENT_AUDIO = False
    _SENT_FINISH = False

    auth_url = build_auth_url(config.base_url, config.api_key, config.api_secret)
    _CLIENT = WebSocketClient(auth_url, config.timeout)
    session_id = str(uuid.uuid4())
    emit_session_started(session_id)
    _STATE.session_started = True


def on_audio_chunk(pcm_bytes: bytes, is_commit: bool) -> None:
    del is_commit
    global _SEQUENCE, _SENT_AUDIO
    if not _CLIENT or not _CONFIG:
        return
    status = 0 if not _SENT_AUDIO else 1
    message = build_audio_message(
        app_id=_CONFIG.app_id,
        res_id=_CONFIG.res_id,
        status=status,
        sequence=_SEQUENCE,
        audio=pcm_bytes,
        iat_parameters=build_iat_parameters(_CONFIG) if not _SENT_AUDIO else None,
    )
    _CLIENT.send_text(json.dumps(message, ensure_ascii=False, separators=(",", ":")))
    _SEQUENCE += 1
    _SENT_AUDIO = True


def on_finish() -> None:
    global _SEQUENCE, _SENT_FINISH
    if not _CLIENT or not _CONFIG:
        return
    if not _STATE.upstream_error and not _STATE.final_received and not _SENT_FINISH:
        message = build_audio_message(
            app_id=_CONFIG.app_id,
            res_id=_CONFIG.res_id,
            status=2,
            sequence=_SEQUENCE,
            audio=b"",
            iat_parameters=None,
        )
        _CLIENT.send_text(json.dumps(message, ensure_ascii=False, separators=(",", ":")))
        _SEQUENCE += 1
        _SENT_FINISH = True


def on_cancel() -> None:
    _STATE.cancelled = True
    cleanup()


def on_upstream_message(raw_msg: str | bytes) -> None:
    if isinstance(raw_msg, str):
        payload = raw_msg.encode("utf-8")
    else:
        payload = raw_msg

    try:
        message = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Invalid iFlytek response JSON: {exc}") from exc
    if not isinstance(message, dict):
        raise RuntimeError("Invalid iFlytek response: expected a JSON object.")

    header = message.get("header")
    if not isinstance(header, dict):
        raise RuntimeError("Invalid iFlytek response: missing header.")
    code = header.get("code", 0)
    if code not in {0, "0", None}:
        details = str(header.get("message", "unknown error"))
        notify_error("err_upstream", code=str(code), detail=details)
        raise RuntimeError(f"iFlytek ASR error {code}: {details}")

    result_container = message.get("payload")
    result = result_container.get("result") if isinstance(result_container, dict) else None
    if not isinstance(result, dict):
        if header.get("status") == 2:
            _STATE.final_received = True
            _FINAL_EVENT.set()
        return

    decoded_result: dict[str, Any] | None = None
    text = result.get("text")
    if isinstance(text, str) and text:
        decoded_result = decode_result_text(text, str(result.get("compress", "raw")))
        update_segments(_STATE.segments, decoded_result)
        current_text = combine_text_fragments([_STATE.segments[idx] for idx in sorted(_STATE.segments)])
        if current_text and current_text != _STATE.latest_text:
            _STATE.latest_text = current_text
            emit_partial(current_text)

    result_status = result.get("status", header.get("status"))
    if result_status in {2, "2"} or decoded_result_is_last(decoded_result):
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
                    if not _STATE.final_received and not _STOP_EVENT.is_set():
                        _STATE.upstream_error = True
                        notify_error("err_network", detail="iFlytek connection closed unexpectedly.")
                        _FINAL_EVENT.set()
                    break
                opcode, payload = message
                if opcode != 0x1:
                    continue
                on_upstream_message(payload)
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
            if _STOP_EVENT.is_set():
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
        if saw_finish and not _STOP_EVENT.is_set():
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
