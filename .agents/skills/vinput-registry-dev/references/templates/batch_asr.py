#!/usr/bin/env python3
"""Canonical Template: VInput Batch ASR Provider (Batch ASR Protocol).

Executes as a one-shot process after recording stops:
- stdin:  Raw uncompressed PCM audio bytes (16000Hz, 1 channel, S16_LE).
- stdout: Plain final transcript text.
- stderr: JSONL desktop notification on error ({"title": "...", "body": "..."}).
- exit:   0 on success, 1 on runtime error, 2 on usage/config error.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass

EXIT_SUCCESS = 0
EXIT_RUNTIME_ERROR = 1
EXIT_USAGE_ERROR = 2

# Extensible multi-language catalog: locale -> {key: template}
MESSAGES: dict[str, dict[str, str]] = {
    "en": {
        "title": "Speech Recognition",
        "missing_api_key": "Missing required API key.",
        "network_error": "Network request failed: {error}",
        "auth_error": "Authentication failed. Please verify your credentials.",
        "server_error": "Server returned error ({status}): {message}",
        "unexpected_error": "Unexpected recognition error: {error}",
    },
    "zh_CN": {
        "title": "语音识别",
        "missing_api_key": "缺少必需的 API Key。",
        "network_error": "网络请求失败: {error}",
        "auth_error": "鉴权失败，请检查认证凭据。",
        "server_error": "服务端返回错误 ({status}): {message}",
        "unexpected_error": "语音识别出现未预期错误: {error}",
    },
}

FALLBACK_LOCALE = "en"


def detect_locale() -> str:
    """Detect system locale from LC_ALL, LC_MESSAGES, or LANG."""
    for var in ("LC_ALL", "LC_MESSAGES", "LANG"):
        val = os.getenv(var, "").strip()
        if val:
            # Normalize e.g. "zh_CN.UTF-8" -> "zh_CN"
            return val.split(".")[0].split("@")[0]
    return FALLBACK_LOCALE


SYSTEM_LOCALE = detect_locale()


def get_message(key: str, **kwargs: object) -> str:
    """Retrieve and format localized string with multi-tier fallback.

    Resolution order:
    1. Exact locale match (e.g. 'zh_CN')
    2. Primary language prefix (e.g. 'zh')
    3. Fallback locale ('en')
    """
    candidates = (SYSTEM_LOCALE, SYSTEM_LOCALE.split("_")[0], FALLBACK_LOCALE)
    for loc in candidates:
        if loc in MESSAGES and key in MESSAGES[loc]:
            template = MESSAGES[loc][key]
            return template.format(**kwargs) if kwargs else template
    return key


def notify_error(key: str, **kwargs: object) -> None:
    """Write a single-line JSONL desktop notification to stderr and flush."""
    title = get_message("title")
    body = get_message(key, **kwargs)
    payload = {"title": title, "body": body}
    sys.stderr.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stderr.flush()


class VInputError(Exception):
    """Base exception carrying exit code and message key."""

    exit_code = EXIT_RUNTIME_ERROR

    def __init__(self, key: str, **kwargs: object) -> None:
        super().__init__(key)
        self.key = key
        self.kwargs = kwargs


class ConfigError(VInputError):
    """Raised when configuration or required environment variable is missing."""

    exit_code = EXIT_USAGE_ERROR


# --- Configuration & Audio Inputs ---


@dataclass
class BatchConfig:
    api_key: str
    endpoint: str


def require_env(name: str) -> str:
    """Read a mandatory environment variable or raise ConfigError."""
    val = os.getenv(name, "").strip()
    if not val:
        raise ConfigError("missing_api_key")
    return val


def optional_env(name: str, default: str = "") -> str:
    """Read an optional string environment variable."""
    return os.getenv(name, default).strip()


def load_config() -> BatchConfig:
    """Parse and validate provider configuration."""
    pass


def read_audio_input() -> bytes:
    """Read raw uncompressed PCM audio bytes from stdin."""
    return sys.stdin.buffer.read()


# --- Business Logic & Output ---


def transcribe(audio: bytes, config: BatchConfig) -> str:
    """Invoke upstream ASR endpoint with raw PCM audio.

    Args:
        audio: Raw PCM bytes (16000Hz, mono, S16_LE).
        config: Provider configuration.

    Returns:
        Final transcript text string.
    """
    pass


def emit_transcript(text: str) -> None:
    """Write plain transcript text to stdout and flush."""
    sys.stdout.write(text.strip())
    sys.stdout.flush()


# --- Execution Flow ---


def run() -> int:
    config = load_config()
    audio = read_audio_input()
    if not audio:
        return EXIT_SUCCESS

    text = transcribe(audio, config)
    emit_transcript(text)
    return EXIT_SUCCESS


def main() -> int:
    try:
        return run()
    except VInputError as exc:
        notify_error(exc.key, **exc.kwargs)
        return exc.exit_code
    except Exception as exc:
        notify_error("unexpected_error", error=str(exc))
        return EXIT_RUNTIME_ERROR


if __name__ == "__main__":
    sys.exit(main())
