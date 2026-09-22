#!/usr/bin/env python3
"""vinput provider: generic local/self-hosted ASR over WebSocket (vLLM /v1/realtime style).

Single connection, 250ms PCM chunks, incremental partial output.
Pure Python standard library (socket+struct+ssl+threading), no pip deps.
"""

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
from typing import Any
from urllib.parse import urlparse

DEFAULT_URL = "ws://127.0.0.1:7000/v1/realtime"
DEFAULT_MODEL = "qwen3-asr"
DEFAULT_TIMEOUT = 30
DEFAULT_FINISH_GRACE_SECS = 0.5
DEFAULT_CHUNK_MS = 250
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
EXIT_RUNTIME_ERROR = 1
EXIT_USAGE_ERROR = 2
ASR_TEXT_TAG = "<asr_text>"


def write_stdout(event: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(event, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def write_stderr(message: str) -> None:
    sys.stderr.write(message + "\n")
    sys.stderr.flush()


def debug_log(message: str) -> None:
    if os.getenv("VINPUT_ASR_DEBUG", "").strip().lower() in {"1", "true", "yes", "on"}:
        sys.stderr.write("[provider.vllm] " + message + "\n")
        sys.stderr.flush()


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
        # Keep any bytes after the HTTP header terminator so an immediate
        # WebSocket frame (e.g. session.created) is not lost.
        head, _, trailing = data.partition(b"\r\n\r\n")
        self._recv_buffer = trailing
        return bytes(head) + b"\r\n\r\n" + trailing

    def _validate_handshake(self, response: bytes, key: str) -> None:
        header_blob = response.split(b"\r\n\r\n", 1)[0].decode("utf-8", errors="replace")
        lines = header_blob.split("\r\n")
        if not lines or "101" not in lines[0]:
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

    def send_json(self, payload: dict[str, Any]) -> None:
        self._send_frame(0x1, json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    def send_binary(self, payload: bytes) -> None:
        self._send_frame(0x2, payload)

    def recv_json(self) -> dict[str, Any] | None:
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
                raise RuntimeError(f"Invalid JSON message from ASR service: {exc}") from exc

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        if self._closed:
            return

        with self._send_lock:
            self._send_frame_unlocked(opcode, payload)

    def _send_frame_unlocked(self, opcode: int, payload: bytes) -> None:
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
        return bytes(data)


def _strip_trailing_lang_prefix(seg: str) -> str:
    """If `seg` ends with a Qwen3-ASR `language <lang>` metadata prefix that is
    immediately followed by the `<asr_text>` marker, remove exactly that run.

    We only remove it when the whole trailing run is `language <word>` (a single
    non-whitespace word), so ordinary dictated text such as 'select language
    English' is never truncated.
    """
    lp = "language "
    pos = seg.rfind(lp)
    if pos < 0:
        return seg
    after = seg[pos + len(lp):]
    # language name must be one non-whitespace word (no spaces/punct/newline)
    if after and after.strip() and not any(c.isspace() for c in after):
        return seg[:pos]
    return seg


def strip_prefix(text: str) -> str:
    """Strip every `language {lang}<asr_text>` prefix, keep all segments, drop newlines.

    For each `<asr_text>` marker, we remove the trailing `language <word>` run
    immediately before it. The final tail after the last marker is kept as-is,
    so ordinary dictated text is preserved.
    """
    out = []
    pos = 0
    while True:
        rel = text.find(ASR_TEXT_TAG, pos)
        if rel < 0:
            # Final tail: keep as-is (no metadata removal here).
            out.append(text[pos:])
            break
        seg = text[pos:rel]
        out.append(_strip_trailing_lang_prefix(seg))
        pos = rel + len(ASR_TEXT_TAG)
    return "".join(out).replace("\n", "").replace("\r", "")


def build_session_update(model: str) -> dict[str, Any]:
    return {"type": "session.update", "model": model}


def build_append(audio_b64: str) -> dict[str, Any]:
    return {"type": "input_audio_buffer.append", "audio": audio_b64}


def build_commit(final: bool) -> dict[str, Any]:
    return {"type": "input_audio_buffer.commit", "final": final}


def handle_server_message(message: dict[str, Any], state: dict[str, Any]) -> None:
    mtype = str(message.get("type", "")).strip()
    if mtype in ("session.created", "session.updated"):
        # session_started was already emitted in run(); just mark ready.
        # Some OpenAI-Realtime-style endpoints acknowledge session.update with
        # session.updated instead of session.created, so accept either.
        state["session_started"] = True
        return
    if mtype == "transcription.delta":
        delta = str(message.get("delta", ""))
        state["raw"] += delta
        cleaned = strip_prefix(state["raw"])
        if cleaned:
            write_stdout({"type": "partial", "text": cleaned})
        return
    if mtype == "transcription.done":
        text = str(message.get("text", ""))
        final = strip_prefix(text) if text else strip_prefix(state["raw"])
        if final and not state.get("final_sent"):
            state["final_sent"] = True
            write_stdout({"type": "final", "text": final})
        state["done"] = True
        return
    if mtype == "error":
        err = message.get("error")
        err_msg = str(err.get("message", "ASR error")) if isinstance(err, dict) else str(err or "ASR error")
        write_stdout({"type": "error", "message": err_msg})
        state["error"] = err_msg
        return


def wait_for_session_ready(state, timeout: float) -> None:
    """Wait until the reader thread has seen session.created, or timeout.

    Avoids sending audio before the upstream session is ready.
    """
    import time as _time
    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        if state.get("session_started"):
            return
        _time.sleep(0.02)
    raise RuntimeError("timed out waiting for session.created")


def run() -> int:
    model = get_optional_env("VINPUT_ASR_MODEL", DEFAULT_MODEL)
    url = get_optional_env("VINPUT_ASR_URL", DEFAULT_URL)
    timeout = get_optional_int_env("VINPUT_ASR_TIMEOUT", DEFAULT_TIMEOUT)
    finish_grace_secs = get_optional_float_env("VINPUT_ASR_FINISH_GRACE_SECS", DEFAULT_FINISH_GRACE_SECS)
    chunk_ms = get_optional_int_env("VINPUT_ASR_CHUNK_MS", DEFAULT_CHUNK_MS)

    debug_log(f"connecting to {url}")
    client = WebSocketClient(url, {}, timeout)
    # vinput daemon expects session_started promptly; send it now.
    write_stdout({"type": "session_started"})
    client.send_json(build_session_update(model))
    client.send_json(build_commit(final=False))
    debug_log("session.update + non-final commit sent")

    state = {"session_started": False, "error": None, "done": False, "raw": "", "closed": False, "final_sent": False}
    stop_event = threading.Event()

    def reader() -> None:
        try:
            while not stop_event.is_set():
                message = client.recv_json()
                if message is None:
                    break
                handle_server_message(message, state)
        except Exception as exc:
            if not stop_event.is_set():
                state["error"] = str(exc)
                write_stdout({"type": "error", "message": str(exc)})
        finally:
            stop_event.set()

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()

    saw_finish = False
    pending_commit = False
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
            etype = str(event.get("type", "")).strip()
            if etype == "audio":
                b64 = event.get("audio_base64")
                if not isinstance(b64, str) or not b64:
                    raise ValueError("audio event requires non-empty audio_base64.")
                # Don't send audio before the upstream session is ready.
                wait_for_session_ready(state, timeout)
                pcm_audio = base64.b64decode(b64)
                pending_commit = True
                step = max(2, int(16000 * chunk_ms / 1000) * 2)
                for i in range(0, len(pcm_audio), step):
                    chunk = pcm_audio[i:i+step]
                    client.send_json(build_append(base64.b64encode(chunk).decode("ascii")))
                if bool(event.get("commit", False)):
                    # This block is already the final chunk; no need to commit
                    # again on finish. Clear the pending flag.
                    client.send_json(build_commit(final=True))
                    pending_commit = False
                continue
            if etype == "finish":
                saw_finish = True
                if pending_commit:
                    client.send_json(build_commit(final=True))
                    # Keep pending_commit true so the finalizer below waits for
                    # transcription.done before closing.
                break
            if etype == "cancel":
                stop_event.set()
                break
            raise ValueError(f"Unsupported event type: {etype or ''}")
    finally:
        if saw_finish and not stop_event.is_set():
            # Wait up to finish_grace_secs for transcription.done to arrive.
            thread.join(timeout=finish_grace_secs)
        stop_event.set()
        client.close()
        thread.join(timeout=1.0)
        if state["raw"] and not state.get("final_sent") and not state.get("error") and state.get("done"):
            final = strip_prefix(state["raw"])
            if final:
                state["final_sent"] = True
                write_stdout({"type": "final", "text": final})
        if not state["closed"]:
            write_stdout({"type": "closed"})
            state["closed"] = True
    if state.get("error"):
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
