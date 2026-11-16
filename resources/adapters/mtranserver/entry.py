#!/usr/bin/env python3
"""Managed local LLM adapter: MTranServer translation bridge.

Translates input transcripts using a local or remote MTranServer instance.
Operates as a command pipe filter (stdin -> stdout) for vinput-daemon.
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass
from urllib.error import URLError
from urllib.request import Request, urlopen

EXIT_SUCCESS = 0
EXIT_RUNTIME_ERROR = 1
EXIT_USAGE_ERROR = 2

DEFAULT_MTRAN_URL = "http://127.0.0.1:8989"

MESSAGES: dict[str, dict[str, str]] = {
    "en_US": {
        "title": "MTranServer Adapter",
        "err_translate_failed": "MTranServer translation failed: {detail}",
        "err_network": "Network error communicating with MTranServer: {detail}",
    },
    "zh_CN": {
        "title": "MTranServer 翻译适配器",
        "err_translate_failed": "MTranServer 翻译失败：{detail}",
        "err_network": "连接 MTranServer 发生网络错误：{detail}",
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

    url: str
    token: str
    default_target_lang: str


def optional_env(name: str, default: str = "") -> str:
    """Read an optional environment variable with fallback."""
    val = os.getenv(name, "").strip()
    return val if val else default


def load_config() -> AdapterConfig:
    """Load and validate all adapter runtime configuration from environment."""
    url = optional_env("MTRAN_URL", DEFAULT_MTRAN_URL).rstrip("/")
    token = optional_env("MTRAN_TOKEN")
    default_lang = optional_env("MTRAN_TARGET_LANG", "en")
    return AdapterConfig(url=url, token=token, default_target_lang=default_lang)


def parse_target_lang(text: str, default_lang: str) -> str:
    """Extract target language from natural language text like 'translate to ja'."""
    match = re.search(r"translate\s+to\s+([\w-]+)", text, re.IGNORECASE)
    return match.group(1) if match else default_lang


def call_mtran(text: str, to_lang: str, config: AdapterConfig) -> str:
    """Call MTranServer /translate endpoint."""
    body = json.dumps({"from": "auto", "to": to_lang, "text": text, "html": False}).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if config.token:
        headers["Authorization"] = f"Bearer {config.token}"

    req = Request(f"{config.url}/translate", data=body, headers=headers, method="POST")
    try:
        with urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return str(data.get("result", ""))
    except URLError as exc:
        raise VInputError(
            "err_network", detail=f"Failed to connect to MTranServer at {config.url}: {exc.reason}"
        ) from exc


def rewrite_text(text: str, config: AdapterConfig) -> str | list[str]:
    """Rewrite raw dictation transcript by translating it."""
    clean = text.strip()
    if not clean:
        return ""
    to_lang = parse_target_lang(clean, config.default_target_lang)
    result = call_mtran(clean, to_lang, config)
    return [result] if result else []


def rewrite_command(text: str, selected: str, config: AdapterConfig) -> str | list[str]:
    """Rewrite text in command mode using selected text as primary context."""
    target_text = selected.strip() if selected.strip() else text.strip()
    if not target_text:
        return ""
    to_lang = parse_target_lang(text, config.default_target_lang)
    result = call_mtran(target_text, to_lang, config)
    return [result] if result else []


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
        notify_error("err_translate_failed", detail=str(exc))
        return EXIT_RUNTIME_ERROR


if __name__ == "__main__":
    sys.exit(main())
