#!/usr/bin/env python3
"""Managed local LLM adapter: CJK Trim.

Removes redundant spaces around CJK punctuation and between consecutive CJK ideographs
that some speech recognition models (e.g. sherpa-onnx Zipformer) insert into transcripts.
Operates as a command pipe filter (stdin -> stdout) for vinput-daemon.
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass

EXIT_SUCCESS = 0
EXIT_RUNTIME_ERROR = 1
EXIT_USAGE_ERROR = 2

# Unicode CJK punctuation codepoint ranges:
#   \u3000-\u303F: CJK Symbols and Punctuation (IDEOGRAPHIC FULL STOP, etc.)
#   \uFF01-\uFF65: Fullwidth ASCII Variants (FULLWIDTH COMMA, EXCLAMATION, etc.)
#   \u2000-\u206F: General Punctuation (HORIZONTAL ELLIPSIS, EM DASH, quotes)
#   \uFE10-\uFE1F: Presentation Forms for Vertical Lines
#   \uFE30-\uFE4F: CJK Compatibility Forms
CJK_PUNCT_RANGE = r"[\u3000-\u303f\uff01-\uff65\u2000-\u206f\ufe10-\ufe1f\ufe30-\ufe4f]"

# CJK Unified Ideographs & Extension ranges:
CJK_IDEOGRAPHS_RANGE = r"[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff]"

MESSAGES: dict[str, dict[str, str]] = {
    "en_US": {
        "title": "CJK Trim Adapter",
        "err_clean_failed": "Failed to trim CJK whitespace: {detail}",
    },
    "zh_CN": {
        "title": "CJK Trim 适配器",
        "err_clean_failed": "清理中文字符多余空格失败：{detail}",
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


@dataclass(frozen=True)
class AdapterConfig:
    """Configuration parsed from environment variables."""

    collapse_cjk_spaces: bool


def optional_bool_env(name: str, default: bool) -> bool:
    """Read an optional boolean environment variable."""
    val = os.getenv(name)
    if val is None or not val.strip():
        return default
    return val.strip().lower() not in {"0", "false", "no", "off"}


def load_config() -> AdapterConfig:
    """Load and validate all adapter runtime configuration from environment."""
    return AdapterConfig(
        collapse_cjk_spaces=optional_bool_env("CJK_TRIM_COLLAPSE_CJK_SPACES", default=True),
    )


def clean_transcript(text: str, collapse_cjk_spaces: bool = True) -> str:
    """Remove redundant spaces around CJK punctuation and between CJK ideographs."""
    if not text:
        return ""

    # 1. Remove whitespace immediately after CJK punctuation
    text = re.sub(f"({CJK_PUNCT_RANGE})\\s+", r"\1", text)

    # 2. Remove whitespace immediately before CJK punctuation
    text = re.sub(f"\\s+({CJK_PUNCT_RANGE})", r"\1", text)

    # 3. Remove spaces between consecutive CJK ideographs if enabled
    if collapse_cjk_spaces:
        while re.search(f"({CJK_IDEOGRAPHS_RANGE})\\s+({CJK_IDEOGRAPHS_RANGE})", text):
            text = re.sub(f"({CJK_IDEOGRAPHS_RANGE})\\s+({CJK_IDEOGRAPHS_RANGE})", r"\1\2", text)

    # 4. Strip leading/trailing whitespace only. Interior ASCII spacing is kept.
    return text.strip()


def rewrite_text(text: str, config: AdapterConfig) -> str | list[str]:
    """Rewrite raw dictation transcript."""
    return clean_transcript(text, collapse_cjk_spaces=config.collapse_cjk_spaces)


def rewrite_command(text: str, selected: str, config: AdapterConfig) -> str | list[str]:
    """Rewrite text in command / instruction mode with optional selected context."""
    return clean_transcript(text, collapse_cjk_spaces=config.collapse_cjk_spaces)


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
        notify_error("err_clean_failed", detail=str(exc))
        return EXIT_RUNTIME_ERROR


if __name__ == "__main__":
    sys.exit(main())
