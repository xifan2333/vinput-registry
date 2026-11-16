#!/usr/bin/env python3
"""Canonical Template: VInput Adapter (Adapter Protocol).

Executes as a one-shot Unix pipe filter:
- stdin:  Raw transcript string (dictation mode) OR
          JSON `{"text": "<asr>", "selected": "<context>"}` (command mode).
- stdout: Rewritten candidate text (single string, or multiple lines if multiple candidates).
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
        "title": "VInput Adapter",
        "missing_env": "Missing required environment variable '{name}'.",
        "invalid_int": "Environment variable '{name}' must be an integer, got '{val}'.",
        "execution_error": "Adapter execution failed: {error}",
        "unexpected_error": "Unexpected adapter error: {error}",
    },
    "zh_CN": {
        "title": "VInput 适配器",
        "missing_env": "缺少必需的环境变量 '{name}'。",
        "invalid_int": "环境变量 '{name}' 必须为整数，当前值为 '{val}'。",
        "execution_error": "适配器执行失败: {error}",
        "unexpected_error": "适配器发生未预期异常: {error}",
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
    """Raised when a required environment variable is missing or invalid."""

    exit_code = EXIT_USAGE_ERROR


# --- Configuration ---


@dataclass
class AdapterConfig:
    pass


def require_env(name: str) -> str:
    """Read a mandatory environment variable or raise ConfigError."""
    val = os.getenv(name, "").strip()
    if not val:
        raise ConfigError("missing_env", name=name)
    return val


def optional_env(name: str, default: str = "") -> str:
    """Read an optional string environment variable."""
    return os.getenv(name, default).strip()


def optional_bool_env(name: str, default: bool) -> bool:
    """Read an optional boolean environment variable."""
    val = os.getenv(name)
    if val is None or not val.strip():
        return default
    return val.strip().lower() not in {"0", "false", "no", "off"}


def load_config() -> AdapterConfig:
    """Parse and validate adapter configuration."""
    pass


# --- Business Logic & Transformation ---


def rewrite_text(text: str, config: AdapterConfig) -> str | list[str]:
    """Transform transcript in dictation mode.

    Args:
        text: Normalized transcript string from stdin.
        config: Loaded adapter configuration.

    Returns:
        Rewritten string (single candidate) or list of candidate strings.
    """
    pass


def rewrite_command(text: str, selected: str, config: AdapterConfig) -> str | list[str]:
    """Transform transcript and context in command mode.

    Args:
        text: Spoken command instruction.
        selected: Highlighted/selected text context.
        config: Loaded adapter configuration.

    Returns:
        Rewritten string or candidate list.
    """
    pass


# --- Execution Flow ---


def run() -> int:
    config = load_config()
    raw_input = sys.stdin.read().strip()
    if not raw_input:
        return EXIT_SUCCESS

    if raw_input.startswith("{") and raw_input.endswith("}"):
        try:
            payload = json.loads(raw_input)
            if isinstance(payload, dict) and "text" in payload and "selected" in payload:
                result = rewrite_command(str(payload.get("text", "")), str(payload.get("selected", "")), config)
            else:
                result = rewrite_text(raw_input, config)
        except json.JSONDecodeError:
            result = rewrite_text(raw_input, config)
    else:
        result = rewrite_text(raw_input, config)

    if isinstance(result, list):
        sys.stdout.write("\n".join(str(item).strip() for item in result if str(item).strip()))
    elif result is not None:
        sys.stdout.write(str(result).strip())
    sys.stdout.flush()
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
