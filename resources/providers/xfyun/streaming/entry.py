#!/usr/bin/env python3

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
from dataclasses import dataclass
from email.utils import formatdate
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

DEFAULT_URL = "wss://iat.cn-huabei-1.xf-yun.com/v1"
DEFAULT_TIMEOUT = 30
DEFAULT_FINISH_GRACE_SECS = 8.0
MAX_FINISH_GRACE_SECS = 8.0
MAX_WEBSOCKET_MESSAGE_BYTES = 4 * 1024 * 1024
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
EXIT_RUNTIME_ERROR = 1
EXIT_USAGE_ERROR = 2

_stdout_lock = threading.Lock()


def write_stdout(event: dict[str, Any]) -> None:
    with _stdout_lock:
        sys.stdout.write(json.dumps(event, ensure_ascii=False) + "\n")
        sys.stdout.flush()


def write_stderr(message: str) -> None:
    sys.stderr.write(message + "\n")
    sys.stderr.flush()


def emit_error(state: "ProviderState", message: str) -> None:
    if state.error:
        return
    state.error = message
    write_stdout({"type": "error", "message": message})


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
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer.") from exc


def get_optional_float_env(name: str, default: float) -> float:
    value = os.getenv(name, "").strip()
    if not value:
        return default
    try:
        return float(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number.") from exc


def get_optional_bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name, "").strip().lower()
    if not value:
        return default
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean.")


def build_auth_url(url: str, api_key: str, api_secret: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"ws", "wss"} or not parsed.hostname:
        raise ValueError("VINPUT_ASR_URL must be a ws:// or wss:// URL.")

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


def json_bool_env(name: str, default: bool) -> bool | None:
    if not os.getenv(name, "").strip():
        return None
    return get_optional_bool_env(name, default)


def build_iat_parameters() -> dict[str, Any]:
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
    if get_optional_bool_env("VINPUT_ASR_ENABLE_WPGS", True):
        iat["dwa"] = "wpgs"

    integer_options = {
        "VINPUT_ASR_BOS": "bos",
        "VINPUT_ASR_VGAP": "vgap",
        "VINPUT_ASR_EOS": "eos",
        "VINPUT_ASR_VINFO": "vinfo",
        "VINPUT_ASR_EVL": "evl",
        "VINPUT_ASR_OPT": "opt",
        "VINPUT_ASR_LTC": "ltc",
        "VINPUT_ASR_ETT": "ett",
    }
    for env_name, parameter_name in integer_options.items():
        if os.getenv(env_name, "").strip():
            iat[parameter_name] = get_optional_int_env(env_name, 0)

    svad = get_optional_int_env("VINPUT_ASR_SVAD", 0)
    if svad not in {0, 1}:
        raise ValueError("VINPUT_ASR_SVAD must be 0 or 1.")
    iat["svad"] = svad

    fa_nbest = json_bool_env("VINPUT_ASR_FA_NBEST", False)
    if fa_nbest is not None:
        iat["fa_nbest"] = fa_nbest

    for env_name, parameter_name in (
        ("VINPUT_ASR_RES_LANGUAGE", "rlang"),
        ("VINPUT_ASR_HOTWORDS", "dhw"),
        ("VINPUT_ASR_CONTEXT", "context"),
    ):
        value = get_optional_env(env_name)
        if value:
            iat[parameter_name] = value

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


def normalize_text(text: str) -> str:
    return text.strip()


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


def is_ascii_word_character(value: str) -> bool:
    return bool(value) and value.isascii() and (value.isalnum() or value == "_")


def combine_text_fragments(fragments: list[str]) -> str:
    combined = ""
    for fragment in fragments:
        fragment = normalize_text(fragment)
        if not fragment:
            continue
        if combined and is_ascii_word_character(combined[-1]) and is_ascii_word_character(fragment[0]):
            combined += " "
        combined += fragment
    return combined


def emit_partial(state: ProviderState, text: str) -> None:
    text = normalize_text(text)
    if not text or text == state.latest_text:
        return
    state.latest_text = text
    write_stdout({"type": "partial", "text": text})


def emit_final(state: ProviderState) -> bool:
    text = normalize_text(state.latest_text)
    if not text or state.final_emitted:
        return False
    write_stdout(
        {
            "type": "final",
            "text": text,
            "segment_final": True,
            "utterance_final": True,
        }
    )
    state.final_emitted = True
    return True


class WebSocketClient:
    def __init__(self, url: str, timeout: int) -> None:
        parsed = urlparse(url)
        if parsed.scheme not in {"ws", "wss"} or not parsed.hostname:
            raise ValueError("Authenticated URL must use ws:// or wss://.")

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


class ActiveStream:
    def __init__(
        self,
        *,
        state: ProviderState,
        url: str,
        app_id: str,
        res_id: str,
        timeout: int,
    ) -> None:
        self.state = state
        self.app_id = app_id
        self.res_id = res_id
        self.sequence = 1
        self.sent_audio = False
        self.sent_finish = False
        self.stop_event = threading.Event()
        self.final_event = threading.Event()
        self.iat_parameters = build_iat_parameters()
        self.client = WebSocketClient(url, timeout)
        self.segments: dict[int, str] = {}
        self.session_id = str(uuid.uuid4())
        self._emit_session_started()
        self.reader = threading.Thread(target=self._reader, daemon=True)
        self.reader.start()

    def _emit_session_started(self) -> None:
        if self.state.session_started:
            return
        write_stdout({"type": "session_started", "session_id": self.session_id, "config": {}})
        self.state.session_started = True

    def send_audio(self, audio: bytes) -> None:
        status = 0 if not self.sent_audio else 1
        message = build_audio_message(
            app_id=self.app_id,
            res_id=self.res_id,
            status=status,
            sequence=self.sequence,
            audio=audio,
            iat_parameters=self.iat_parameters if not self.sent_audio else None,
        )
        self.client.send_text(json.dumps(message, ensure_ascii=False, separators=(",", ":")))
        self.sequence += 1
        self.sent_audio = True

    def finish(self, grace_secs: float) -> bool:
        try:
            if not self.state.upstream_error and not self.state.final_received and not self.sent_finish:
                message = build_audio_message(
                    app_id=self.app_id,
                    res_id=self.res_id,
                    status=2,
                    sequence=self.sequence,
                    audio=b"",
                    iat_parameters=None,
                )
                self.client.send_text(json.dumps(message, ensure_ascii=False, separators=(",", ":")))
                self.sequence += 1
                self.sent_finish = True
            if not self.state.upstream_error and not self.state.final_received:
                if not self.final_event.wait(timeout=grace_secs):
                    emit_error(self.state, "Timed out waiting for the iFlytek final recognition result.")
            return self.state.final_received or self.state.upstream_error
        finally:
            self.stop_event.set()
            self.client.close()
            self.reader.join(timeout=1.0)

    def cancel(self) -> None:
        self.state.cancelled = True
        self.stop_event.set()
        self.client.close()
        self.reader.join(timeout=1.0)

    def _handle_message(self, payload: bytes) -> None:
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
            details = header.get("message", "unknown error")
            raise RuntimeError(f"iFlytek ASR error {code}: {details}")

        result_container = message.get("payload")
        result = result_container.get("result") if isinstance(result_container, dict) else None
        if not isinstance(result, dict):
            if header.get("status") == 2:
                self.state.final_received = True
                self.final_event.set()
            return

        decoded_result: dict[str, Any] | None = None
        text = result.get("text")
        if isinstance(text, str) and text:
            decoded_result = self._decode_result_text(text, result.get("compress", "raw"))
            self._update_segments(decoded_result)
            emit_partial(
                self.state,
                combine_text_fragments([self.segments[index] for index in sorted(self.segments)]),
            )

        result_status = result.get("status", header.get("status"))
        if result_status in {2, "2"} or decoded_result_is_last(decoded_result):
            self.state.final_received = True
            self.final_event.set()

    def _decode_result_text(self, text: str, compression: str) -> dict[str, Any]:
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

    def _update_segments(self, result: dict[str, Any]) -> None:
        sequence = result.get("sn")
        if not isinstance(sequence, int):
            sequence = max(self.segments, default=0) + 1

        pgs = result.get("pgs")
        if pgs == "rpl":
            replacement_range = result.get("rg")
            if isinstance(replacement_range, list) and len(replacement_range) == 2:
                try:
                    start, end = int(replacement_range[0]), int(replacement_range[1])
                except (TypeError, ValueError):
                    start, end = sequence, sequence
                for index in range(start, end + 1):
                    self.segments.pop(index, None)

        self.segments[sequence] = extract_result_text(result)

    def _reader(self) -> None:
        try:
            while not self.stop_event.is_set():
                message = self.client.recv_message()
                if message is None:
                    if not self.state.final_received and not self.stop_event.is_set():
                        self.state.upstream_error = True
                        emit_error(self.state, "iFlytek WebSocket closed before the final recognition result.")
                        self.final_event.set()
                    break
                opcode, payload = message
                if opcode != 0x1:
                    continue
                self._handle_message(payload)
        except Exception as exc:
            if not self.stop_event.is_set():
                self.state.upstream_error = True
                emit_error(self.state, str(exc))
                self.final_event.set()
        finally:
            self.stop_event.set()


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


def run() -> int:
    app_id = get_required_env("VINPUT_ASR_APP_ID")
    api_key = get_required_env("VINPUT_ASR_API_KEY")
    api_secret = get_required_env("VINPUT_ASR_API_SECRET")
    base_url = get_optional_env("VINPUT_ASR_URL", DEFAULT_URL)
    res_id = get_optional_env("VINPUT_ASR_RES_ID")
    timeout = get_optional_int_env("VINPUT_ASR_TIMEOUT", DEFAULT_TIMEOUT)
    grace_secs = get_optional_float_env("VINPUT_ASR_FINISH_GRACE_SECS", DEFAULT_FINISH_GRACE_SECS)
    if timeout <= 0:
        raise ValueError("VINPUT_ASR_TIMEOUT must be greater than zero.")
    if grace_secs < 0 or grace_secs > MAX_FINISH_GRACE_SECS:
        raise ValueError("VINPUT_ASR_FINISH_GRACE_SECS must be between 0 and 8 seconds.")

    state = ProviderState()
    active_stream: ActiveStream | None = None
    try:
        for raw_line in sys.stdin:
            line = raw_line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON input: {exc}") from exc
            if not isinstance(event, dict):
                raise ValueError("Provider input must be a JSON object.")

            event_type = str(event.get("type", "")).strip()
            if event_type == "audio":
                audio_base64 = event.get("audio_base64")
                if not isinstance(audio_base64, str) or not audio_base64:
                    raise ValueError("audio event requires non-empty audio_base64.")
                try:
                    audio = base64.b64decode(audio_base64, validate=True)
                except ValueError as exc:
                    raise ValueError("audio_base64 is not valid base64.") from exc
                if not audio:
                    raise ValueError("audio event decoded to empty audio.")
                if active_stream is None:
                    auth_url = build_auth_url(base_url, api_key, api_secret)
                    active_stream = ActiveStream(
                        state=state,
                        url=auth_url,
                        app_id=app_id,
                        res_id=res_id,
                        timeout=timeout,
                    )
                active_stream.send_audio(audio)
                # The daemon's commit flag is only an upstream hint. The vinput
                # utterance ends on the separate finish event, not on a chunk commit.
                continue

            if event_type == "finish":
                if active_stream is not None:
                    should_emit_final = active_stream.finish(grace_secs)
                    if should_emit_final:
                        emit_final(state)
                    active_stream = None
                break

            if event_type == "cancel":
                state.cancelled = True
                if active_stream is not None:
                    active_stream.cancel()
                    active_stream = None
                break

            raise ValueError(f"Unsupported event type: {event_type or '<missing>'}")
    except Exception as exc:
        emit_error(state, str(exc))
        if active_stream is not None:
            try:
                active_stream.cancel()
            except Exception:
                pass
            active_stream = None
    finally:
        if active_stream is not None and not state.cancelled:
            try:
                should_emit_final = active_stream.finish(grace_secs)
                if should_emit_final:
                    emit_final(state)
            except Exception as exc:
                emit_error(state, str(exc))
                try:
                    active_stream.cancel()
                except Exception:
                    pass
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
