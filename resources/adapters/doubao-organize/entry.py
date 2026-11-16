#!/usr/bin/env python3
"""Managed local LLM adapter: Doubao IME AI text organization.

Connects to Doubao IME AI text organization service to polish and structure ASR transcripts.
Operates as a command pipe filter (stdin -> stdout) for vinput-daemon.
"""

from __future__ import annotations

import base64
import codecs
import fcntl
import gzip
import hashlib
import json
import os
import secrets
import sys
import tempfile
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

EXIT_SUCCESS = 0
EXIT_RUNTIME_ERROR = 1
EXIT_USAGE_ERROR = 2

REGISTER_URL = "https://log.snssdk.com/service/2/device_register/"
TNC_URL = "https://tnc3-bjlgy.snssdk.com/get_domains/v5/"
ORGANIZE_URL = "https://ime.doubao.com/api/v2/ai/text_organization"
CACHE_TTL = 10800.0
PRESERVED = frozenset("]})>》”'\"】’％）%℃:：")
DEFAULT_IDENTITY = Path.home() / ".cache/vinput/doubaoime/credentials.json"
IDENTITY_FIELDS = ("device_id", "install_id", "cdid", "openudid", "clientudid")

MESSAGES: dict[str, dict[str, str]] = {
    "en_US": {
        "title": "Doubao Text Organize Adapter",
        "err_organize_failed": "Doubao IME text organization failed: {detail}",
        "err_identity_failed": "Failed to initialize Doubao IME identity: {detail}",
    },
    "zh_CN": {
        "title": "豆包文本整理适配器",
        "err_organize_failed": "豆包输入法文本整理失败：{detail}",
        "err_identity_failed": "初始化豆包输入法凭据失败：{detail}",
    },
}


class VInputError(Exception):
    """Base exception for structured vinput provider/adapter errors."""

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


def _chacha20_xor(data: bytes, key: bytes, nonce: bytes, counter: int = 0) -> bytes:
    if len(key) != 32 or len(nonce) != 12:
        raise ValueError("ChaCha20 requires a 32-byte key and 12-byte nonce")

    def rotate(value: int, amount: int) -> int:
        return ((value << amount) | (value >> (32 - amount))) & 0xFFFFFFFF

    def quarter(state: list[int], a: int, b: int, c: int, d: int) -> None:
        state[a] = (state[a] + state[b]) & 0xFFFFFFFF
        state[d] = rotate(state[d] ^ state[a], 16)
        state[c] = (state[c] + state[d]) & 0xFFFFFFFF
        state[b] = rotate(state[b] ^ state[c], 12)
        state[a] = (state[a] + state[b]) & 0xFFFFFFFF
        state[d] = rotate(state[d] ^ state[a], 8)
        state[c] = (state[c] + state[d]) & 0xFFFFFFFF
        state[b] = rotate(state[b] ^ state[c], 7)

    constants = b"expand 32-byte k"

    def words(raw: bytes) -> list[int]:
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


def encode_tt_encrypt(gzip_bytes: bytes, salt: bytes | None = None) -> bytes:
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
        "url": REGISTER_URL,
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
    did = str(body.get("device_id_str") or body.get("device_id") or "")
    iid = str(body.get("install_id_str") or body.get("install_id") or "")
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


def _ensure_identity(path: Path) -> dict[str, Any]:
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


def _remove_trailing(text: str) -> str:
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


class CryptoCache:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.value: tuple[str, bytes] | None = None
        self.expires = 0.0

    def get(self, params: dict[str, Any]) -> tuple[str, bytes]:
        with self.lock:
            if self.value is not None and time.monotonic() < self.expires:
                return self.value
            query = {**params, "abi": "arm64-v8a", "tnc_src": "4", "okhttp_version": "4.2.243.10-wave"}
            request = urllib.request.Request(TNC_URL + "?" + urllib.parse.urlencode(query))
            with urllib.request.urlopen(request, timeout=8.0) as response:
                body = json.loads(response.read())
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

    def invalidate(self) -> None:
        with self.lock:
            self.value = None
            self.expires = 0.0


def _handle_sse_frame(event: str, data_str: str, text: str) -> str | None:
    """Return the final organized text for a terminating SSE frame, else None."""
    if data_str == "[DONE]" or event == "done":
        return text
    try:
        payload = json.loads(data_str)
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    if event == "scene.error" or payload.get("no_rewrite") is True:
        return text
    content = payload.get("content")
    if event == "scene.completed" and isinstance(content, str) and content.strip():
        return content.strip()
    return None


def _organize(text: str, state: dict[str, Any], cache: CryptoCache) -> str:
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
    ticket, key = cache.get(params)
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
    with urllib.request.urlopen(request, timeout=8.0) as response:
        if response.headers.get("x-tt-e-r", "0;0") != "0;0":
            cache.invalidate()
        encrypted = response.headers.get("x-tt-e-b") == "1"
        resp_nonce = base64.b64decode(response.headers["x-tt-e-p"], validate=True) if encrypted else b""
        decoder = codecs.getincrementaldecoder("utf-8")()
        offset = 0
        pending = ""
        event = ""
        data_parts: list[str] = []
        while True:
            chunk = response.read(4096)
            if not chunk:
                break
            if encrypted:
                skip = offset % 64
                decoded = _chacha20_xor(bytes(skip) + chunk, key, resp_nonce, offset // 64)[skip:]
                offset += len(chunk)
                chunk = decoded
            pending += decoder.decode(chunk)
            while "\n" in pending:
                line, pending = pending.split("\n", 1)
                line = line.removesuffix("\r").strip()
                if not line:
                    if data_parts:
                        result = _handle_sse_frame(event, "\n".join(data_parts), text)
                        data_parts = []
                        if result is not None:
                            return result
                    event = ""
                    continue
                if line.startswith("event:"):
                    event = line[6:].strip()
                    continue
                if line.startswith("data:"):
                    data_parts.append(line[5:].strip())
    return text


@dataclass(frozen=True)
class AdapterConfig:
    """Configuration parsed from environment variables."""

    identity_path: Path
    no_trailing_punctuation: bool


def optional_env(name: str, default: str = "") -> str:
    """Read an optional environment variable with fallback."""
    val = os.getenv(name, "").strip()
    return val if val else default


def optional_bool_env(name: str, default: bool) -> bool:
    """Read an optional boolean environment variable."""
    val = os.getenv(name)
    if val is None or not val.strip():
        return default
    return val.strip().lower() not in {"0", "false", "no", "off"}


def load_config() -> AdapterConfig:
    """Load and validate all adapter runtime configuration from environment."""
    raw_path = optional_env("DOUBAOIME_IDENTITY_PATH")
    identity_path = Path(raw_path) if raw_path else DEFAULT_IDENTITY
    no_trailing = optional_bool_env("DOUBAOIME_NO_TRAILING_PUNCTUATION", default=False)
    return AdapterConfig(identity_path=identity_path, no_trailing_punctuation=no_trailing)


_STATE_CACHE: tuple[dict[str, Any], CryptoCache] | None = None


def get_runtime_state(config: AdapterConfig) -> tuple[dict[str, Any], CryptoCache]:
    """Retrieve or initialize identity credentials and crypto cache."""
    global _STATE_CACHE
    if _STATE_CACHE is None:
        identity = _ensure_identity(config.identity_path)
        crypto = CryptoCache()
        _STATE_CACHE = (identity, crypto)
    return _STATE_CACHE


def rewrite_text(text: str, config: AdapterConfig) -> str | list[str]:
    """Rewrite raw dictation transcript."""
    clean = text.strip()
    if not clean:
        return ""
    identity, crypto = get_runtime_state(config)
    try:
        organized = _organize(clean, identity, crypto)
        if config.no_trailing_punctuation:
            organized = _remove_trailing(organized)
        return [organized] if organized else [clean]
    except Exception as exc:
        notify_error("err_organize_failed", detail=str(exc))
        return [clean]


def rewrite_command(text: str, selected: str, config: AdapterConfig) -> str | list[str]:
    """Rewrite text in command mode using selected text as primary context."""
    target_text = selected.strip() if selected.strip() else text.strip()
    return rewrite_text(target_text, config)


def run() -> int:
    """Main filter loop reading stdin and writing candidates to stdout."""
    config = load_config()

    raw_input = sys.stdin.read()
    if not raw_input:
        return EXIT_SUCCESS

    is_json = False
    text = raw_input
    selected = ""

    try:
        data = json.loads(raw_input)
        if isinstance(data, dict) and "text" in data:
            is_json = True
            text = str(data.get("text", ""))
            selected = str(data.get("selected_text", ""))
    except Exception:
        is_json = False

    if is_json and selected:
        output = rewrite_command(text, selected, config)
    else:
        output = rewrite_text(text, config)

    if isinstance(output, list):
        sys.stdout.write(json.dumps({"candidates": output}, ensure_ascii=False) + "\n")
    else:
        sys.stdout.write(f"{output}\n")
    sys.stdout.flush()
    return EXIT_SUCCESS


def main() -> int:
    """CLI wrapper returning standard exit code."""
    try:
        return run()
    except VInputError as exc:
        notify_error(exc.key, **exc.kwargs)
        return exc.exit_code
    except Exception as exc:
        notify_error("err_organize_failed", detail=str(exc))
        return EXIT_RUNTIME_ERROR


if __name__ == "__main__":
    sys.exit(main())
