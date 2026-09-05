#!/usr/bin/env python3
"""Registry integrity and consistency validator for vinput-registry.

Validates:
1. JSON syntax across registry/ and i18n/
2. Schema requirements for providers.json, models.json, and adapters.json
3. Local script file and README existence for managed resources
4. Fallback URL mirrors (raw.githubusercontent.com, gh-proxy.com, ghfast.top)
5. i18n key parity and completeness for all registered items
"""

import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
REGISTRY_DIR = ROOT / "registry"
I18N_DIR = ROOT / "i18n"
RESOURCES_DIR = ROOT / "resources"

EXPECTED_MIRRORS = [
    "https://raw.githubusercontent.com/xifan2333/vinput-registry/main/",
    "https://gh-proxy.com/https://raw.githubusercontent.com/xifan2333/vinput-registry/main/",
    "https://ghfast.top/https://raw.githubusercontent.com/xifan2333/vinput-registry/main/",
]


def load_json(path: Path) -> Any:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        print(f"Error loading {path}: {exc}", file=sys.stderr)
        sys.exit(1)


def validate_mirrors(urls: list[str], item_id: str) -> list[str]:
    errors = []
    if len(urls) < 3:
        errors.append(f"[{item_id}] script_urls must contain at least 3 mirrors, got {len(urls)}")
        return errors

    for i, prefix in enumerate(EXPECTED_MIRRORS):
        if not urls[i].startswith(prefix):
            errors.append(f"[{item_id}] mirror #{i + 1} expected to start with '{prefix}', got '{urls[i]}'")
    return errors


def validate_providers() -> tuple[list[str], set[str]]:
    errors = []
    ids = set()
    data = load_json(REGISTRY_DIR / "providers.json")
    items = data.get("items", [])

    for item in items:
        item_id = item.get("id", "")
        if not item_id:
            errors.append("Found provider item without 'id'")
            continue
        if item_id in ids:
            errors.append(f"Duplicate provider id: {item_id}")
        ids.add(item_id)

        # Provider ID format: provider.<folder>.<name>
        if not re.match(r"^provider\.[a-zA-Z0-9_\-.]+$", item_id):
            errors.append(f"[{item_id}] Invalid provider id format, expected 'provider.<folder>.<name>'")

        # Streaming ID rule
        stream = item.get("stream", False)
        if stream and not item_id.endswith(".streaming"):
            errors.append(f"[{item_id}] Streaming provider id must end with '.streaming'")

        short_id = item.get("short_id", "")
        if not short_id:
            errors.append(f"[{item_id}] Missing 'short_id'")

        # Local files check
        # id: provider.<folder>.<name> -> resources/providers/<folder>/<name>/
        parts = item_id.split(".", 1)[1]  # <folder>.<name>
        folder, name = parts.split(".", 1) if "." in parts else (parts, "")
        if name:
            script_path = RESOURCES_DIR / "providers" / folder / name / "entry.py"
            readme_path = RESOURCES_DIR / "providers" / folder / name / "README.md"
            if not script_path.is_file():
                errors.append(f"[{item_id}] Missing local entry file: {script_path.relative_to(ROOT)}")
            if not readme_path.is_file():
                errors.append(f"[{item_id}] Missing local README file: {readme_path.relative_to(ROOT)}")

        script_urls = item.get("script_urls", [])
        errors.extend(validate_mirrors(script_urls, item_id))

        # Validate envs
        for env in item.get("envs", []):
            env_name = env.get("name", "")
            if not env_name.startswith("VINPUT_ASR_"):
                errors.append(f"[{item_id}] Env '{env_name}' violates VINPUT_ASR_* namespace convention")
            if "required" not in env or not isinstance(env["required"], bool):
                errors.append(f"[{item_id}] Env '{env_name}' missing boolean 'required' field")

    return errors, ids


def validate_adapters() -> tuple[list[str], set[str]]:
    errors = []
    ids = set()
    data = load_json(REGISTRY_DIR / "adapters.json")
    items = data.get("items", [])

    for item in items:
        item_id = item.get("id", "")
        if not item_id:
            errors.append("Found adapter item without 'id'")
            continue
        if item_id in ids:
            errors.append(f"Duplicate adapter id: {item_id}")
        ids.add(item_id)

        if not re.match(r"^adapter\.[a-zA-Z0-9_\-.]+$", item_id):
            errors.append(f"[{item_id}] Invalid adapter id format, expected 'adapter.<folder>.<name>'")

        short_id = item.get("short_id", "")
        if not short_id:
            errors.append(f"[{item_id}] Missing 'short_id'")

        parts = item_id.split(".", 1)[1]
        folder, name = parts.split(".", 1) if "." in parts else (parts, "")
        if name:
            script_path = RESOURCES_DIR / "adapters" / folder / name / "entry.py"
            readme_path = RESOURCES_DIR / "adapters" / folder / name / "README.md"
            if not script_path.is_file():
                errors.append(f"[{item_id}] Missing local entry file: {script_path.relative_to(ROOT)}")
            if not readme_path.is_file():
                errors.append(f"[{item_id}] Missing local README file: {readme_path.relative_to(ROOT)}")

        script_urls = item.get("script_urls", [])
        errors.extend(validate_mirrors(script_urls, item_id))

        for env in item.get("envs", []):
            env_name = env.get("name", "")
            if not env_name:
                errors.append(f"[{item_id}] Adapter env has empty name")
            if "required" not in env or not isinstance(env["required"], bool):
                errors.append(f"[{item_id}] Env '{env_name}' missing boolean 'required' field")

    return errors, ids


def validate_models() -> tuple[list[str], set[str]]:
    errors = []
    ids = set()
    data = load_json(REGISTRY_DIR / "models.json")
    items = data.get("items", [])

    for item in items:
        item_id = item.get("id", "")
        if not item_id:
            errors.append("Found model item without 'id'")
            continue
        if item_id in ids:
            errors.append(f"Duplicate model id: {item_id}")
        ids.add(item_id)

        if not re.match(r"^model\.[a-zA-Z0-9_\-.]+$", item_id):
            errors.append(f"[{item_id}] Invalid model id format, expected 'model.<backend>.<name>'")

        short_id = item.get("short_id", "")
        if not short_id:
            errors.append(f"[{item_id}] Missing 'short_id'")

        urls = item.get("urls", [])
        if not urls:
            errors.append(f"[{item_id}] Missing download 'urls'")

        sha256 = item.get("sha256", "")
        if not sha256 or len(sha256) != 64:
            errors.append(f"[{item_id}] Invalid or missing sha256 checksum: '{sha256}'")

        if item.get("size_bytes", 0) <= 0:
            errors.append(f"[{item_id}] Missing or non-positive 'size_bytes'")

        if not item.get("language"):
            errors.append(f"[{item_id}] Missing 'language' field")

        # Validate vinput_model
        vm = item.get("vinput_model")
        if not vm or not isinstance(vm, dict):
            errors.append(f"[{item_id}] Missing or non-object 'vinput_model'")
            continue

        backend = vm.get("backend", "")
        runtime = vm.get("runtime", "")
        if backend not in ("sherpa-offline", "sherpa-streaming"):
            errors.append(
                f"[{item_id}] vinput_model.backend must be sherpa-offline or sherpa-streaming, got '{backend}'"
            )
        if backend == "sherpa-offline" and runtime != "offline":
            errors.append(
                f"[{item_id}] vinput_model.backend sherpa-offline requires runtime 'offline', got '{runtime}'"
            )
        if backend == "sherpa-streaming" and runtime != "online":
            errors.append(
                f"[{item_id}] vinput_model.backend sherpa-streaming requires runtime 'online', got '{runtime}'"
            )

        if not vm.get("family"):
            errors.append(f"[{item_id}] vinput_model missing required 'family' field")

        if not isinstance(vm.get("recognizer"), dict):
            errors.append(f"[{item_id}] vinput_model missing 'recognizer' object")
        if not isinstance(vm.get("model"), dict):
            errors.append(f"[{item_id}] vinput_model missing 'model' object")

    return errors, ids


def validate_i18n(all_ids: set[str]) -> list[str]:
    errors = []
    en_path = I18N_DIR / "en_US.json"
    zh_path = I18N_DIR / "zh_CN.json"

    en = load_json(en_path)
    zh = load_json(zh_path)

    # Check that each registered ID has title & description in both languages
    for res_id in all_ids:
        title_key = f"{res_id}.title"
        desc_key = f"{res_id}.description"

        if title_key not in en:
            errors.append(f"[i18n] Missing '{title_key}' in {en_path.name}")
        if desc_key not in en:
            errors.append(f"[i18n] Missing '{desc_key}' in {en_path.name}")
        if title_key not in zh:
            errors.append(f"[i18n] Missing '{title_key}' in {zh_path.name}")
        if desc_key not in zh:
            errors.append(f"[i18n] Missing '{desc_key}' in {zh_path.name}")

    # Check key parity between en_US and zh_CN
    en_keys = set(en.keys())
    zh_keys = set(zh.keys())
    only_in_en = en_keys - zh_keys
    only_in_zh = zh_keys - en_keys

    for k in only_in_en:
        errors.append(f"[i18n] Key '{k}' present in {en_path.name} but missing in {zh_path.name}")
    for k in only_in_zh:
        errors.append(f"[i18n] Key '{k}' present in {zh_path.name} but missing in {en_path.name}")

    return errors


def main() -> int:
    all_errors = []

    provider_errors, provider_ids = validate_providers()
    adapter_errors, adapter_ids = validate_adapters()
    model_errors, model_ids = validate_models()

    all_errors.extend(provider_errors)
    all_errors.extend(adapter_errors)
    all_errors.extend(model_errors)

    all_ids = provider_ids | adapter_ids | model_ids
    i18n_errors = validate_i18n(all_ids)
    all_errors.extend(i18n_errors)

    if all_errors:
        print(f"Validation failed with {len(all_errors)} error(s):", file=sys.stderr)
        for err in all_errors:
            print(f"  - {err}", file=sys.stderr)
        return 1

    print(
        f"Validation passed: {len(provider_ids)} providers, {len(adapter_ids)} adapters, "
        f"{len(model_ids)} models. All files and i18n keys verified."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
