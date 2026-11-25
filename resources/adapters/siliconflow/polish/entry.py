#!/usr/bin/env python3
"""Managed local LLM adapter: OpenAI-compatible text polishing bridge.

Polishes and refines ASR transcripts using any OpenAI-compatible chat
completions API (OpenAI, SiliconFlow, DeepSeek, Moonshot, vLLM, etc.).
Operates as a command pipe filter (stdin -> stdout) for vinput-daemon.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

EXIT_SUCCESS = 0
EXIT_RUNTIME_ERROR = 1
EXIT_USAGE_ERROR = 2

DEFAULT_OPENAI_URL = "https://api.openai.com/v1/chat/completions"
DEFAULT_OPENAI_MODEL = "gpt-4o-mini"
DEFAULT_POLISH_SYSTEM_PROMPT = (
    "你是一名专业的文本润色助手。请对用户提供的语音识别文本进行润色，"
    "修正错别字、去除口语化冗余、优化标点与语句通顺度，"
    "保持原意不变，直接输出润色后的文本，不要添加任何解释或前缀。"
)

MESSAGES: dict[str, dict[str, str]] = {
    "en_US": {
        "title": "OpenAI-Compatible Polish Adapter",
        "missing_env": "Missing required environment variable '{name}'.",
        "err_polish_failed": "Text polishing failed: {detail}",
        "err_network": "Network error communicating with the LLM API: {detail}",
        "err_empty_response": "The LLM API returned an empty response.",
        "err_invalid_content": "The LLM API returned invalid message content.",
    },
    "zh_CN": {
        "title": "OpenAI 兼容润色适配器",
        "missing_env": "缺少必需的环境变量 '{name}'。",
        "err_polish_failed": "文本润色失败：{detail}",
        "err_network": "连接 LLM API 发生网络错误：{detail}",
        "err_empty_response": "LLM API 返回了空响应。",
        "err_invalid_content": "LLM API 返回的消息内容格式无效。",
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


def require_env(name: str) -> str:
    """Read a mandatory environment variable or raise ConfigError."""
    val = os.getenv(name, "").strip()
    if not val:
        raise ConfigError("missing_env", name=name)
    return val


def optional_env(name: str, default: str = "") -> str:
    """Read an optional environment variable with fallback."""
    val = os.getenv(name, "").strip()
    return val if val else default


@dataclass(frozen=True)
class AdapterConfig:
    """Configuration parsed from environment variables."""

    url: str
    api_key: str
    model: str
    system_prompt: str
    temperature: float


def load_config() -> AdapterConfig:
    """Load and validate all adapter runtime configuration from environment."""
    url = optional_env("OPENAI_API_URL", DEFAULT_OPENAI_URL).rstrip("/")
    api_key = require_env("OPENAI_API_KEY")
    model = optional_env("OPENAI_MODEL", DEFAULT_OPENAI_MODEL)
    system_prompt = optional_env("OPENAI_SYSTEM_PROMPT", DEFAULT_POLISH_SYSTEM_PROMPT)
    temperature_raw = optional_env("OPENAI_TEMPERATURE", "0.3")
    try:
        temperature = float(temperature_raw)
    except (TypeError, ValueError):
        temperature = 0.3
    return AdapterConfig(
        url=url,
        api_key=api_key,
        model=model,
        system_prompt=system_prompt,
        temperature=temperature,
    )


def _is_url_safe_for_credentials(url: str) -> bool:
    """Return True if the URL may carry the API key over the wire.

    Allows https:// endpoints and http:// only for loopback addresses
    (127.0.0.1 / localhost), so local self-hosted gateways (vLLM, etc.)
    remain usable while plaintext remote HTTP is rejected.
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    if parsed.scheme == "https":
        return bool(parsed.netloc)
    if parsed.scheme == "http":
        host = (parsed.hostname or "").lower()
        return host in ("127.0.0.1", "localhost", "::1", "0.0.0.0")
    return False


def call_llm(text: str, config: AdapterConfig) -> str:
    """Call an OpenAI-compatible chat completions endpoint for text polishing."""
    payload = {
        "model": config.model,
        "messages": [
            {"role": "system", "content": config.system_prompt},
            {"role": "user", "content": text},
        ],
        "temperature": config.temperature,
        "stream": False,
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if not _is_url_safe_for_credentials(config.url):
        raise VInputError(
            "err_network",
            detail=f"Refusing to send API key over plaintext HTTP to {config.url}; "
                   "use https:// or a loopback (127.0.0.1/localhost) endpoint",
        )
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {config.api_key}",
    }

    req = Request(config.url, data=body, headers=headers, method="POST")
    try:
        with urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except URLError as exc:
        raise VInputError(
            "err_network",
            detail=f"Failed to connect to LLM API at {config.url}: {exc.reason}",
        ) from exc

    try:
        choices = data.get("choices") or []
        message = choices[0].get("message", {}) if choices else {}
    except (IndexError, AttributeError, TypeError) as exc:
        raise VInputError("err_polish_failed", detail=f"Unexpected response shape: {exc}") from exc

    content = message.get("content")
    if not isinstance(content, str):
        raise VInputError("err_invalid_content")
    result = content.strip()

    if not result:
        raise VInputError("err_empty_response")
    return result


def rewrite_text(text: str, config: AdapterConfig) -> str | list[str]:
    """Rewrite raw dictation transcript by polishing it via the LLM API."""
    clean = text.strip()
    if not clean:
        return ""
    result = call_llm(clean, config)
    return [result] if result else []


def rewrite_command(text: str, selected: str, config: AdapterConfig) -> str | list[str]:
    """Rewrite text in command mode using selected text as primary context."""
    target_text = selected.strip() if selected.strip() else text.strip()
    if not target_text:
        return ""
    result = call_llm(target_text, config)
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
        notify_error("err_polish_failed", detail=str(exc))
        return EXIT_RUNTIME_ERROR


if __name__ == "__main__":
    sys.exit(main())
