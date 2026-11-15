#!/usr/bin/env python3
"""AssemblyAI batch speech-to-text provider."""

from __future__ import annotations

import json
import os
import sys
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

DEFAULT_TIMEOUT = 60
DEFAULT_URL = "https://dictation.assemblyai.com/v1/transcribe"
SAMPLE_RATE = 16000
CHANNELS = 1

EXIT_SUCCESS = 0
EXIT_RUNTIME_ERROR = 1
EXIT_USAGE_ERROR = 2

MESSAGES: dict[str, dict[str, str]] = {
    "en_US": {
        "title": "AssemblyAI Speech Recognition",
        "err_no_audio": "No audio received on stdin.",
        "err_missing_api_key": "Missing VINPUT_ASR_API_KEY.",
        "err_http": "AssemblyAI HTTP {code}: {message}",
        "err_network": "Failed to reach AssemblyAI: {detail}",
        "err_empty_transcript": "AssemblyAI returned an empty transcript.",
        "err_transcribe_failed": "Transcription failed: {detail}",
    },
    "zh_CN": {
        "title": "AssemblyAI 语音识别",
        "err_no_audio": "标准输入未接收到音频数据。",
        "err_missing_api_key": "缺少 VINPUT_ASR_API_KEY 环境变量。",
        "err_http": "AssemblyAI HTTP {code}：{message}",
        "err_network": "连接 AssemblyAI 失败：{detail}",
        "err_empty_transcript": "AssemblyAI 返回了空白识别结果。",
        "err_transcribe_failed": "语音识别失败：{detail}",
    },
}


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


class VInputError(Exception):
    """Base exception carrying exit code and message key."""

    exit_code = EXIT_RUNTIME_ERROR

    def __init__(self, key: str, **kwargs: object) -> None:
        super().__init__(key)
        self.key = key
        self.kwargs = kwargs


class ConfigError(VInputError):
    """Raised when a required environment variable is missing or invalid."""

    exit_code = EXIT_USAGE_ERROR


@dataclass(frozen=True)
class ProviderConfig:
    """Parsed provider configuration from environment variables."""

    api_key: str
    endpoint: str
    timeout: int
    language: str | None
    verbatim: bool


def require_env(name: str) -> str:
    """Read a mandatory environment variable or raise ConfigError."""
    val = os.getenv(name, "").strip()
    if not val:
        raise ConfigError("err_missing_api_key")
    return val
    return val


def optional_env(name: str, default: str = "") -> str:
    """Read an optional environment variable with fallback."""
    val = os.getenv(name, "").strip()
    return val if val else default


def optional_int_env(name: str, default: int) -> int:
    """Read an optional integer environment variable with fallback."""
    val = os.getenv(name, "").strip()
    if not val:
        return default
    try:
        return int(val)
    except ValueError:
        return default


def optional_bool_env(name: str, default: bool) -> bool:
    """Read an optional boolean environment variable."""
    val = os.getenv(name)
    if val is None or not val.strip():
        return default
    return val.strip().lower() not in {"0", "false", "no", "off"}


def load_config() -> ProviderConfig:
    """Load and validate all configuration from environment variables."""
    api_key = require_env("VINPUT_ASR_API_KEY")
    endpoint = optional_env("VINPUT_ASR_URL", DEFAULT_URL)
    timeout = optional_int_env("VINPUT_ASR_TIMEOUT", DEFAULT_TIMEOUT)
    language = optional_env("VINPUT_ASR_LANGUAGE") or None
    verbatim = optional_bool_env("VINPUT_ASR_ASSEMBLYAI_VERBATIM", False)
    return ProviderConfig(
        api_key=api_key,
        endpoint=endpoint,
        timeout=timeout,
        language=language,
        verbatim=verbatim,
    )


def read_audio_input() -> bytes:
    """Read complete raw PCM audio from stdin buffer."""
    pcm_audio = sys.stdin.buffer.read()
    if not pcm_audio:
        raise VInputError("err_no_audio")
    return pcm_audio


def build_config(language: str | None) -> dict[str, object]:
    """Build AssemblyAI request config."""
    config: dict[str, object] = {"sample_rate": SAMPLE_RATE, "channels": CHANNELS}
    if language:
        config["language_codes"] = [language]
    return config


def build_multipart(parts: Iterable[tuple[str, str | None, str, bytes]]) -> tuple[bytes, str]:
    """Construct multipart/form-data body."""
    boundary = f"----vinput-{uuid.uuid4().hex}"
    body = bytearray()

    for name, filename, content_type, content in parts:
        body.extend(f"--{boundary}\r\n".encode())
        disposition = f'Content-Disposition: form-data; name="{name}"'
        if filename:
            disposition += f'; filename="{filename}"'
        body.extend(f"{disposition}\r\n".encode())
        body.extend(f"Content-Type: {content_type}\r\n\r\n".encode())
        body.extend(content)
        body.extend(b"\r\n")

    body.extend(f"--{boundary}--\r\n".encode())
    return bytes(body), boundary


def parse_error_payload(payload: bytes) -> str:
    """Extract readable error message from error body."""
    text = payload.decode("utf-8", errors="replace").strip()
    if not text:
        return "Empty error response from AssemblyAI."

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return text

    if isinstance(data, dict):
        for key in ("error", "detail", "message"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return text


def pick_text(data: dict[str, object], verbatim: bool) -> str:
    """Select verbatim or cleanup text from transcript response."""
    candidates = ["text"] if verbatim else ["llm_response", "text"]
    for key in candidates:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    raise VInputError("err_empty_transcript")


def transcribe(audio_bytes: bytes, config: ProviderConfig) -> str:
    """Execute batch transcription request."""
    body, boundary = build_multipart(
        [
            ("config", None, "application/json", json.dumps(build_config(config.language)).encode()),
            ("audio", "audio.pcm", "audio/pcm", audio_bytes),
        ]
    )

    request = Request(
        config.endpoint,
        data=body,
        headers={
            "Authorization": config.api_key,
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Accept": "application/json",
        },
        method="POST",
    )

    try:
        with urlopen(request, timeout=config.timeout) as response:
            data = json.loads(response.read())
    except HTTPError as exc:
        payload = exc.read()
        msg = parse_error_payload(payload)
        raise VInputError("err_http", code=exc.code, message=msg) from exc
    except URLError as exc:
        raise VInputError("err_network", detail=str(exc.reason)) from exc
    except VInputError:
        raise
    except Exception as exc:
        raise VInputError("err_transcribe_failed", detail=str(exc)) from exc

    if not isinstance(data, dict):
        raise VInputError("err_transcribe_failed", detail="Unexpected response from AssemblyAI.")
    return pick_text(data, config.verbatim)


def emit_transcript(text: str) -> None:
    """Write recognized transcript string to stdout."""
    sys.stdout.write(f"{text}\n")
    sys.stdout.flush()


def run() -> int:
    """Main execution flow: load config, read audio, transcribe, emit result."""
    config = load_config()
    pcm_audio = read_audio_input()
    transcript = transcribe(pcm_audio, config)
    emit_transcript(transcript)
    return EXIT_SUCCESS


def main() -> int:
    """CLI wrapper returning standard exit code."""
    try:
        return run()
    except VInputError as exc:
        notify_error(exc.key, **exc.kwargs)
        return exc.exit_code
    except Exception as exc:
        notify_error("err_transcribe_failed", detail=str(exc))
        return EXIT_RUNTIME_ERROR


if __name__ == "__main__":
    sys.exit(main())
