#!/usr/bin/env python3

import argparse
import base64
import codecs
import fcntl
import gzip
import hashlib
import http.server
import json
import os
import secrets
import select
import socket
import sys
import tempfile
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import cast
from urllib.parse import urlencode
from urllib.request import Request, urlopen

MODEL = "doubaoime-organize"
SCENE_PROMPT = "<vinput-asr>\n{{asr}}\n</vinput-asr>"
REGISTER_URL = "https://log.snssdk.com/service/2/device_register/"
TNC_URL = "https://tnc3-bjlgy.snssdk.com/get_domains/v5/"
ORGANIZE_URL = "https://ime.doubao.com/api/v2/ai/text_organization"
CACHE_TTL = 10800.0
APP_KEY = "OrnqKvSSrs"
PRESERVED = frozenset("]})>》”'\"】’％）%℃:：")


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


def _chacha20_xor(data, key, nonce, counter=0):
    if len(key) != 32 or len(nonce) != 12:
        raise ValueError("ChaCha20 requires a 32-byte key and 12-byte nonce")

    def rotate(value, amount):
        return ((value << amount) | (value >> (32 - amount))) & 0xFFFFFFFF

    def quarter(state, a, b, c, d):
        state[a] = (state[a] + state[b]) & 0xFFFFFFFF
        state[d] = rotate(state[d] ^ state[a], 16)
        state[c] = (state[c] + state[d]) & 0xFFFFFFFF
        state[b] = rotate(state[b] ^ state[c], 12)
        state[a] = (state[a] + state[b]) & 0xFFFFFFFF
        state[d] = rotate(state[d] ^ state[a], 8)
        state[c] = (state[c] + state[d]) & 0xFFFFFFFF
        state[b] = rotate(state[b] ^ state[c], 7)

    constants = b"expand 32-byte k"

    def words(raw):
        return [int.from_bytes(raw[index : index + 4], "little") for index in range(0, len(raw), 4)]

    base = words(constants) + words(key) + [counter] + words(nonce)
    result = bytearray()
    for offset in range(0, len(data), 64):
        state = base[:]
        for _ in range(10):
            quarter(state, 0, 4, 8, 12)
            quarter(state, 1, 5, 9, 13)
            quarter(state, 2, 6, 10, 14)
            quarter(state, 3, 7, 11, 15)
            quarter(state, 0, 5, 10, 15)
            quarter(state, 1, 6, 11, 12)
            quarter(state, 2, 7, 8, 13)
            quarter(state, 3, 4, 9, 14)
        stream = b"".join(((state[index] + base[index]) & 0xFFFFFFFF).to_bytes(4, "little") for index in range(16))
        result.extend(left ^ right for left, right in zip(data[offset : offset + 64], stream, strict=False))
        base[12] = (base[12] + 1) & 0xFFFFFFFF
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


def _remove_trailing(text):
    lines = text.splitlines(keepends=True)
    result = []
    for line in lines:
        ending = ""
        if line.endswith("\r\n"):
            ending, line = "\r\n", line[:-2]
        elif line.endswith("\n") or line.endswith("\r"):
            ending, line = line[-1], line[:-1]
        while line and line[-1] not in PRESERVED and unicodedata.category(line[-1]).startswith("P"):
            line = line[:-1]
        result.append(line + ending)
    return "".join(result)


def _extract_text(body):
    if body.get("model") != MODEL or body.get("stream", False) is not False:
        raise ValueError("Only the dedicated organization model is supported")
    messages = body.get("messages")
    if not isinstance(messages, list) or len(messages) != 1 or messages[0].get("role") != "user":
        raise ValueError("Expected one vinput user message")
    content = messages[0].get("content")
    prefix = "<vinput-asr>\n"
    if not isinstance(content, str) or not content.startswith(prefix):
        raise ValueError("Expected the vinput text wrapper")
    text, marker, suffix = content[len(prefix) :].rpartition("\n</vinput-asr>")
    if not marker or not suffix.startswith("\n\n## Constraints\n") or not text.strip():
        raise ValueError("Missing vinput text wrapper")
    return text


class RequestWork:
    def __init__(self, deadline):
        self.deadline = deadline
        self.cancelled = threading.Event()
        self.lock = threading.Lock()
        self.response = None

    def abort(self):
        self.cancelled.set()
        with self.lock:
            if self.response is not None:
                try:
                    self.response.fp.raw._sock.shutdown(socket.SHUT_RDWR)
                except (AttributeError, OSError):
                    pass

    def check(self):
        if self.cancelled.is_set():
            raise ConnectionAbortedError("Local request disconnected")
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Organization deadline expired")
        return remaining

    def open(self, request, timeout):
        self.check()
        response = urllib.request.urlopen(request, timeout=min(timeout, self.check()))
        with self.lock:
            self.response = response
        if self.cancelled.is_set():
            self.abort()
            response.close()
            raise ConnectionAbortedError("Local request disconnected")
        return response

    def release(self):
        with self.lock:
            self.response = None


class CryptoCache:
    def __init__(self):
        self.lock = threading.Lock()
        self.value = None
        self.expires = 0.0

    def get(self, params, work):
        if not self.lock.acquire(timeout=work.check()):
            raise TimeoutError("Key initialization deadline expired")
        try:
            work.check()
            if self.value is not None and time.monotonic() < self.expires:
                return self.value
            query = {**params, "abi": "arm64-v8a", "tnc_src": "4", "okhttp_version": "4.2.243.10-wave"}
            request = urllib.request.Request(TNC_URL + "?" + urllib.parse.urlencode(query))
            try:
                with work.open(request, work.check()) as response:
                    body = json.loads(response.read())
            finally:
                work.release()
            config = body["data"]["encrypt_config_v1"]
            if not config.get("enabled") or config.get("ver") != 3:
                raise ValueError("TTNet fallback encryption is unavailable")
            ticket = config["bk_t"]
            key = base64.b64decode(config["bk_k"], validate=True)
            if not isinstance(ticket, str) or not ticket or len(key) != 32:
                raise ValueError("Invalid TTNet fallback configuration")
            self.value = ticket, key
            interval = float(body["data"].get("tnc_update_interval", CACHE_TTL))
            self.expires = time.monotonic() + min(CACHE_TTL, interval if interval > 0 else CACHE_TTL)
            return self.value
        finally:
            self.lock.release()

    def invalidate(self):
        with self.lock:
            self.value = None
            self.expires = 0.0


def _sse_lines(response, key, work, first_deadline):
    encrypted = response.headers.get("x-tt-e-b") == "1"
    nonce = base64.b64decode(response.headers["x-tt-e-p"], validate=True) if encrypted else b""
    if encrypted and len(nonce) != 12:
        raise ValueError("Invalid response nonce")
    decoder = codecs.getincrementaldecoder("utf-8")()
    offset = 0
    pending = ""
    event = ""
    data = []
    size = 0
    first = True
    while True:
        remaining = work.check()
        if first:
            remaining = min(remaining, first_deadline - time.monotonic())
        if remaining <= 0:
            raise TimeoutError("No organization event before deadline")
        response.fp.raw._sock.settimeout(remaining)
        chunk = response.read1(4096)
        if not chunk:
            return
        if encrypted:
            skip = offset % 64
            decoded = _chacha20_xor(bytes(skip) + chunk, key, nonce, offset // 64)[skip:]
            offset += len(chunk)
            chunk = decoded
        pending += decoder.decode(chunk)
        while "\n" in pending:
            line, pending = pending.split("\n", 1)
            line = line.removesuffix("\r")
            if len(line) > 1_000_000:
                raise ValueError("SSE line exceeds 1 MB")
            if not line:
                if data:
                    value = "\n".join(data)
                    yield event, value
                    if event in {"scene.delta", "scene.completed", "scene.error"}:
                        try:
                            first = not isinstance(json.loads(value), dict) and first
                        except ValueError:
                            pass
                event, data, size = "", [], 0
            elif not line.startswith(":"):
                field, _, value = line.partition(":")
                value = value.removeprefix(" ")
                if field == "event":
                    event = value
                elif field == "data":
                    size += len(value)
                    if size > 1_000_000:
                        raise ValueError("SSE event exceeds 1 MB")
                    data.append(value)
        if len(pending) > 1_000_000:
            raise ValueError("SSE line exceeds 1 MB")


def _organize(text, state, cache, work):
    params = {
        "aid": "401734",
        "app_name": "oime",
        "channel": "official",
        "version_name": "1.5.1",
        "version_code": "100501006",
        "update_version_code": "100501006",
        "manifest_version_code": "100501006",
        "device_platform": "android",
        "os": "android",
        "os_api": "34",
        "os_version": "14",
        "device_type": "Python protocol test",
        "device_brand": "generic",
        "language": "zh",
        "ssmix": "a",
        "_rticket": str(int(time.time() * 1000)),
        "device_id": state["device_id"],
        "iid": state["install_id"],
        "cdid": state.get("cdid", ""),
        "use-olympus-account": "1",
    }
    try:
        ticket, key = cache.get(params, work)
        body = json.dumps(
            {"scene": 6, "query": text, "space_at_cn_en_nb": 1, "space_at_newline": 1, "stream": True},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
        nonce = secrets.token_bytes(12)
        wire = _chacha20_xor(body, key, nonce)
        headers = {
            "Accept": "text/event-stream",
            "Content-Type": "application/json",
            "sdk-version": "2",
            "passport-sdk-version": "605195",
            "x-tt-e-b": "1",
            "x-tt-e-t": ticket,
            "x-tt-e-p": base64.b64encode(nonce).decode(),
            "x-tt-e-h": base64.b64encode(_chacha20_xor(b"hello", key, nonce)).decode(),
        }
        request = urllib.request.Request(
            ORGANIZE_URL + "?" + urllib.parse.urlencode(params), data=wire, headers=headers, method="POST"
        )
        first_deadline = time.monotonic() + 2.0
        try:
            with work.open(request, 2.0) as response:
                if response.headers.get("x-tt-e-r", "0;0") != "0;0":
                    cache.invalidate()
                for event, data in _sse_lines(response, key, work, first_deadline):
                    if event == "done" or data == "[DONE]":
                        return text
                    if event not in {"scene.delta", "scene.completed", "scene.error"}:
                        continue
                    try:
                        value = json.loads(data)
                    except ValueError:
                        continue
                    if not isinstance(value, dict):
                        continue
                    if event == "scene.error" or value.get("no_rewrite") is True:
                        return text
                    if event == "scene.completed":
                        content = value.get("content")
                        return content if isinstance(content, str) and content.strip() else text
                return text
        finally:
            work.release()
    except urllib.error.HTTPError:
        cache.invalidate()
        return text
    except (OSError, ValueError, KeyError, TimeoutError):
        return text


def _chat_response(text):
    return {
        "id": "chatcmpl-" + uuid.uuid4().hex,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": MODEL,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": json.dumps({"candidates": [text]}, ensure_ascii=False)},
                "finish_reason": "stop",
            }
        ],
    }


class OrganizationServer(http.server.ThreadingHTTPServer):
    identity: dict
    no_trailing: bool
    crypto: CryptoCache


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.rstrip("/") != "/v1/models":
            self.send_error(404)
            return
        self._send_json({"object": "list", "data": [{"id": MODEL, "object": "model", "owned_by": "doubaoime"}]})

    def do_POST(self):
        if self.path.rstrip("/") != "/v1/chat/completions":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 1_000_000:
                raise ValueError("Request body must be between 1 byte and 1 MB")
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError("Expected a JSON object")
            text = _extract_text(body)
        except (ValueError, KeyError, TypeError, AttributeError) as error:
            self._send_json({"error": {"message": str(error)}}, 400)
            return
        work = RequestWork(time.monotonic() + 8.0)
        server = cast(OrganizationServer, self.server)
        completed = threading.Event()
        output = [text]

        def organize():
            try:
                output[0] = _organize(text, server.identity, server.crypto, work)
            finally:
                completed.set()

        worker = threading.Thread(target=organize, daemon=True)
        worker.start()
        try:
            while not completed.wait(0.05):
                if time.monotonic() >= work.deadline:
                    work.abort()
                    break
                readable, _, _ = select.select([self.connection], [], [], 0)
                if readable and not self.connection.recv(1, socket.MSG_PEEK):
                    work.abort()
                    return
            result = output[0] if completed.is_set() else text
            if server.no_trailing:
                result = _remove_trailing(result)
            self._send_json(_chat_response(result))
        except (BrokenPipeError, ConnectionResetError, OSError):
            work.abort()
        finally:
            work.abort()
            worker.join(timeout=0.2)

    def _send_json(self, payload, status=200):
        wire = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(wire)))
        self.end_headers()
        self.wfile.write(wire)

    def log_message(self, format: str, *args: object) -> None:
        return


def main():
    parser = argparse.ArgumentParser(description="Doubao IME organization adapter")
    parser.add_argument("--identity", type=Path, default=Path(os.getenv("DOUBAOIME_IDENTITY_PATH") or DEFAULT_IDENTITY))
    parser.add_argument(
        "--no-trailing-punctuation",
        action="store_true",
        default=os.getenv("DOUBAOIME_NO_TRAILING_PUNCTUATION", "").lower() in {"1", "true", "yes"},
    )
    args = parser.parse_args()
    raw_port = os.getenv("DOUBAOIME_PORT", "").strip()
    if not raw_port:
        sys.stderr.write("[doubaoime-organize] Missing DOUBAOIME_PORT environment variable.\n")
        return 2
    try:
        port = int(raw_port)
        if not 1 <= port <= 65535:
            raise ValueError
    except ValueError:
        sys.stderr.write("[doubaoime-organize] DOUBAOIME_PORT must be an integer between 1 and 65535.\n")
        return 2
    server = OrganizationServer(("127.0.0.1", port), Handler)
    server.identity = _ensure_identity(args.identity)
    server.no_trailing = args.no_trailing_punctuation
    server.crypto = CryptoCache()
    server.daemon_threads = True
    server.serve_forever()


if __name__ == "__main__":
    sys.exit(main())
