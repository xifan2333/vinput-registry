#!/usr/bin/env python3
"""
Upstream Model Monitor for vinput-registry.

Monitors upstream model releases (e.g. k2-fsa/sherpa-onnx tag: asr-models)
for PC-compatible (x86 & ARM CPU) ASR models released after a baseline date (--since).
Only alerts when genuine new models or version updates are published.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sys
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

# Supported model archive extensions
MODEL_ARCHIVE_EXTENSIONS = (
    ".tar.bz2",
    ".tar.gz",
    ".tar.xz",
    ".zip",
    ".tar",
    ".tgz",
)

# Patterns for non-PC specialized embedded / hardware NPU models to exclude
NON_PC_PATTERNS = [
    r"[-_](qnn|rk3588|rk3566|rk3576|rk3562|ascend\d*|ax650\w*|horizon|mthreads|sophgo|cvi|esp32)[-_.]",
    r"^(qnn|rk3588|rk3576|ascend|ax650)",
]

# Known model families and keyword matching
FAMILY_PATTERNS = [
    (r"sense-voice|sense_voice", "sense_voice"),
    (r"qwen3-asr|qwen3_asr|qwen3", "qwen3_asr"),
    (r"fire-red-asr|fire_red_asr|fireredasr", "fire_red_asr"),
    (r"moonshine", "moonshine"),
    (r"zipformer.*transducer|transducer.*zipformer", "transducer"),
    (r"zipformer.*ctc|ctc.*zipformer", "zipformer2_ctc"),
    (r"paraformer", "paraformer"),
    (r"whisper", "whisper"),
    (r"nemo", "nemo_ctc"),
    (r"telespeech", "telespeech_ctc"),
    (r"transducer", "transducer"),
    (r"ctc", "zipformer2_ctc"),
]

# Language patterns
LANGUAGE_PATTERNS = [
    (r"zh-en|zh_en|bilingual.*zh.*en|zh.*en", "zh_en"),
    (r"zh-en-ja-ko-yue|multilingual|14-lang", "multilingual"),
    (r"cantonese|yue", "yue"),
    (r"[\b\-_]zh[\b\-_]|chinese", "zh"),
    (r"[\b\-_]en[\b\-_]|english", "en"),
    (r"[\b\-_]ja[\b\-_]|japanese", "ja"),
    (r"[\b\-_]ko[\b\-_]|korean", "ko"),
    (r"[\b\-_]fr[\b\-_]|french", "fr"),
    (r"[\b\-_]de[\b\-_]|german", "de"),
    (r"[\b\-_]es[\b\-_]|spanish", "es"),
    (r"[\b\-_]ru[\b\-_]|russian", "ru"),
]


@dataclass
class UpstreamAsset:
    name: str
    download_url: str
    size_bytes: int
    created_at: str
    updated_at: str
    release_tag: str
    repo: str
    # Parsed fields
    base_name: str = ""
    date_version: str = ""
    extension: str = ""
    language: str = "zh_en"
    runtime: str = "offline"
    family: str = "transducer"
    quantization: str = "int8"
    supports_hotwords: bool = False
    suggested_id: str = ""
    suggested_short_id: str = ""


@dataclass
class LocalModel:
    id: str
    short_id: str
    urls: List[str]
    size_bytes: int
    language: str
    sha256: str
    filename: str = ""
    base_name: str = ""
    date_version: str = ""
    runtime: str = "offline"
    family: str = ""


@dataclass
class ModelDiffItem:
    asset: UpstreamAsset
    local_match: Optional[LocalModel] = None
    status: str = "new"  # 'new' | 'updated' | 'up_to_date'
    reason: str = ""


def format_size(size_bytes: int) -> str:
    """Format bytes into human-readable string."""
    if size_bytes <= 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    i = 0
    size = float(size_bytes)
    while size >= 1024.0 and i < len(units) - 1:
        size /= 1024.0
        i += 1
    return f"{size:.2f} {units[i]}"


def strip_archive_extension(filename: str) -> Tuple[str, str]:
    """Strip known archive extension and return (stem, ext)."""
    for ext in sorted(MODEL_ARCHIVE_EXTENSIONS, key=len, reverse=True):
        if filename.endswith(ext):
            return filename[: -len(ext)], ext
    return filename, ""


def is_pc_compatible(filename: str) -> bool:
    """
    Check if the model archive is for standard PC (x86 & ARM CPU).
    Filters out specialized embedded NPU/DSP builds (e.g. RK3588, QNN, Ascend).
    """
    name_lower = filename.lower()
    for pat in NON_PC_PATTERNS:
        if re.search(pat, name_lower):
            return False
    return True


def parse_date_version(stem: str) -> Tuple[str, str]:
    """
    Extract date suffix like -2026-06-03 or -20250909 or -2026-03-25.
    Returns (base_name_without_date, date_str).
    """
    m = re.search(r"[-_](\d{4}[-_]\d{2}[-_]\d{2})$", stem)
    if m:
        date_str = m.group(1).replace("_", "-")
        base = stem[: m.start()]
        return base, date_str
    m2 = re.search(r"[-_](\d{8})$", stem)
    if m2:
        date_str = m2.group(1)
        base = stem[: m2.start()]
        return base, date_str
    return stem, ""


def resolve_since_date(since_input: Optional[str]) -> str:
    """
    Resolve --since date string into YYYY-MM-DD.
    Accepts 'today', 'YYYY-MM-DD', 'Nd' (e.g. '7d', '30d'), or defaults to today's date.
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    if not since_input or since_input.strip() == "" or since_input == "today":
        return now.strftime("%Y-%m-%d")

    val = since_input.strip().lower()
    if val.endswith("d") and val[:-1].isdigit():
        days = int(val[:-1])
        return (now - datetime.timedelta(days=days)).strftime("%Y-%m-%d")

    m = re.match(r"^(\d{4}-\d{2}-\d{2})", val)
    if m:
        return m.group(1)

    return now.strftime("%Y-%m-%d")


def infer_model_metadata(filename: str, size_bytes: int) -> Dict[str, Any]:
    """Infer metadata from model archive filename."""
    stem, ext = strip_archive_extension(filename)
    base_name, date_version = parse_date_version(stem)
    name_lower = filename.lower()

    # Runtime
    runtime = "online" if ("streaming" in name_lower or "online" in name_lower) else "offline"

    # Family
    family = "transducer"
    for pattern, fam in FAMILY_PATTERNS:
        if re.search(pattern, name_lower):
            family = fam
            break

    # Language
    language = "zh_en"
    for pattern, lang in LANGUAGE_PATTERNS:
        if re.search(pattern, name_lower):
            language = lang
            break

    # Quantization
    quantization = "fp32"
    if "int8" in name_lower:
        quantization = "int8"
    elif "int4" in name_lower:
        quantization = "int4"
    elif "fp16" in name_lower:
        quantization = "fp16"

    # Supports hotwords
    supports_hotwords = family in ("transducer", "zipformer")

    # Suggested ID
    clean_base = base_name.lower().replace("sherpa-onnx-", "")
    suggested_id = f"model.sherpa-onnx.{clean_base}"

    # Suggested Short ID
    short_parts = ["onnx"]
    if "streaming" in clean_base or "stream" in clean_base:
        short_parts.append("stream")
    elif "sense-voice" in clean_base or "sense_voice" in clean_base:
        short_parts.append("sv")
    elif "qwen3" in clean_base:
        short_parts.append("qwen3")
    elif "moonshine" in clean_base:
        short_parts.append("ms")
    elif "fire-red" in clean_base:
        short_parts.append("firered")
    elif "zipformer" in clean_base:
        short_parts.append("zf")

    short_parts.append(language.replace("_", "-"))
    if quantization != "fp32":
        short_parts.append(quantization)
    short_parts.append("off" if runtime == "offline" else "stream")

    dedup_parts: List[str] = []
    for p in short_parts:
        if not dedup_parts or dedup_parts[-1] != p:
            dedup_parts.append(p)
    suggested_short_id = "-".join(dedup_parts)

    return {
        "base_name": base_name,
        "date_version": date_version,
        "extension": ext,
        "language": language,
        "runtime": runtime,
        "family": family,
        "quantization": quantization,
        "supports_hotwords": supports_hotwords,
        "suggested_id": suggested_id,
        "suggested_short_id": suggested_short_id,
    }


def fetch_github_api(
    endpoint: str,
    token: Optional[str] = None,
) -> Any:
    """Send GET request to GitHub API."""
    url = f"https://api.github.com{endpoint}" if endpoint.startswith("/") else endpoint
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "vinput-registry-monitor",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_release_assets_paginated(
    repo: str,
    tag: str,
    token: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Fetch all release assets with pagination."""
    rel = fetch_github_api(f"/repos/{repo}/releases/tags/{tag}", token=token)
    release_id = rel.get("id")
    if not release_id:
        return rel.get("assets", [])

    all_assets: List[Dict[str, Any]] = []
    page = 1
    while True:
        page_assets = fetch_github_api(
            f"/repos/{repo}/releases/{release_id}/assets?per_page=100&page={page}",
            token=token,
        )
        if not page_assets or not isinstance(page_assets, list):
            break
        all_assets.extend(page_assets)
        if len(page_assets) < 100:
            break
        page += 1

    return all_assets


def fetch_upstream_assets(
    repo: str,
    tags: List[str],
    token: Optional[str] = None,
) -> List[UpstreamAsset]:
    """Fetch PC-compatible model release assets from GitHub repository."""
    assets: List[UpstreamAsset] = []
    seen_names: Set[str] = set()

    for tag in tags:
        print(f"Fetching release assets for {repo} tag: {tag}...")
        try:
            rel_assets = fetch_release_assets_paginated(repo=repo, tag=tag, token=token)
            print(f"Found {len(rel_assets)} raw assets in tag {tag}.")
            for a in rel_assets:
                name = a.get("name", "")
                if not any(name.endswith(ext) for ext in MODEL_ARCHIVE_EXTENSIONS):
                    continue
                if not is_pc_compatible(name):
                    continue
                if name in seen_names:
                    continue
                seen_names.add(name)

                size = a.get("size", 0)
                meta = infer_model_metadata(name, size)
                asset = UpstreamAsset(
                    name=name,
                    download_url=a.get("browser_download_url", ""),
                    size_bytes=size,
                    created_at=a.get("created_at", ""),
                    updated_at=a.get("updated_at", ""),
                    release_tag=tag,
                    repo=repo,
                    base_name=meta["base_name"],
                    date_version=meta["date_version"],
                    extension=meta["extension"],
                    language=meta["language"],
                    runtime=meta["runtime"],
                    family=meta["family"],
                    quantization=meta["quantization"],
                    supports_hotwords=meta["supports_hotwords"],
                    suggested_id=meta["suggested_id"],
                    suggested_short_id=meta["suggested_short_id"],
                )
                assets.append(asset)
        except urllib.error.HTTPError as e:
            print(f"Warning: HTTP error fetching tag {tag} from {repo}: {e.code} {e.reason}", file=sys.stderr)
        except Exception as e:
            print(f"Warning: Failed to fetch tag {tag} from {repo}: {e}", file=sys.stderr)

    return assets


def load_local_registry(models_path: Path) -> List[LocalModel]:
    """Load and parse local models.json."""
    if not models_path.exists():
        print(f"Warning: {models_path} not found.", file=sys.stderr)
        return []

    with open(models_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    models: List[LocalModel] = []
    for item in data.get("items", []):
        urls = item.get("urls", [])
        primary_url = urls[0] if urls else ""
        filename = primary_url.split("/")[-1] if primary_url else ""
        stem, _ = strip_archive_extension(filename)
        base_name, date_version = parse_date_version(stem)

        vm = item.get("vinput_model", {})
        models.append(
            LocalModel(
                id=item.get("id", ""),
                short_id=item.get("short_id", ""),
                urls=urls,
                size_bytes=item.get("size_bytes", 0),
                language=item.get("language", ""),
                sha256=item.get("sha256", ""),
                filename=filename,
                base_name=base_name,
                date_version=date_version,
                runtime=vm.get("runtime", "offline"),
                family=vm.get("family", ""),
            )
        )
    return models


def normalize_base_name(name: str) -> str:
    """Normalize base name for fuzzy comparison."""
    n = name.lower()
    n = n.replace("sherpa-onnx-", "").replace("sherpa_onnx_", "")
    n = re.sub(r"[-_.]", "", n)
    return n


def get_asset_effective_date(asset: UpstreamAsset) -> str:
    """Get the effective date for an upstream asset (from version tag or upload date)."""
    if asset.date_version:
        return asset.date_version
    if asset.created_at:
        return asset.created_at[:10]
    if asset.updated_at:
        return asset.updated_at[:10]
    return ""


def compare_models(
    local_models: List[LocalModel],
    upstream_assets: List[UpstreamAsset],
    since_date: str = "",
) -> List[ModelDiffItem]:
    """
    Compare upstream assets against local models.
    Filters out historical models published before `since_date`.
    """
    diff_items: List[ModelDiffItem] = []

    exact_filenames = {m.filename: m for m in local_models if m.filename}
    base_name_map: Dict[str, LocalModel] = {}
    normalized_map: Dict[str, LocalModel] = {}
    id_map: Dict[str, LocalModel] = {m.id: m for m in local_models}

    for m in local_models:
        if m.base_name:
            base_name_map[m.base_name] = m
            normalized_map[normalize_base_name(m.base_name)] = m

    for asset in upstream_assets:
        eff_date = get_asset_effective_date(asset)

        # 1. Exact filename match
        if asset.name in exact_filenames:
            m = exact_filenames[asset.name]
            diff_items.append(
                ModelDiffItem(
                    asset=asset,
                    local_match=m,
                    status="up_to_date",
                    reason="Exact filename and version match in local registry.",
                )
            )
            continue

        # 2. Check if same base model exists with a different date version
        matched_local: Optional[LocalModel] = None
        if asset.base_name in base_name_map:
            matched_local = base_name_map[asset.base_name]
        elif normalize_base_name(asset.base_name) in normalized_map:
            matched_local = normalized_map[normalize_base_name(asset.base_name)]
        elif asset.suggested_id in id_map:
            matched_local = id_map[asset.suggested_id]

        if matched_local:
            if asset.date_version and matched_local.date_version:
                if asset.date_version > matched_local.date_version:
                    reason = (
                        f"Upstream has newer version: {asset.date_version} "
                        f"(Local: {matched_local.date_version})"
                    )
                    diff_items.append(
                        ModelDiffItem(
                            asset=asset,
                            local_match=matched_local,
                            status="updated",
                            reason=reason,
                        )
                    )
                else:
                    diff_items.append(
                        ModelDiffItem(
                            asset=asset,
                            local_match=matched_local,
                            status="up_to_date",
                            reason="Local version is up to date or newer.",
                        )
                    )
            else:
                if since_date and eff_date and eff_date < since_date:
                    diff_items.append(
                        ModelDiffItem(
                            asset=asset,
                            local_match=matched_local,
                            status="up_to_date",
                            reason="Historical asset prior to baseline date.",
                        )
                    )
                else:
                    diff_items.append(
                        ModelDiffItem(
                            asset=asset,
                            local_match=matched_local,
                            status="updated",
                            reason=f"Matching model base found ({matched_local.id}), candidate update.",
                        )
                    )
        else:
            # 3. Model not in local registry
            if since_date and eff_date and eff_date < since_date:
                continue

            diff_items.append(
                ModelDiffItem(
                    asset=asset,
                    local_match=None,
                    status="new",
                    reason="New model asset published after baseline date.",
                )
            )

    return diff_items


def build_draft_model_json(asset: UpstreamAsset) -> Dict[str, Any]:
    """Generate a draft registry item for models.json."""
    gh_url = asset.download_url
    urls = [
        gh_url,
        f"https://gh-proxy.com/{gh_url}",
        f"https://ghfast.top/{gh_url}",
    ]

    backend = "sherpa-streaming" if asset.runtime == "online" else "sherpa-offline"

    model_config: Dict[str, Any] = {
        "tokens": "tokens.txt",
        "num_threads": 1,
        "debug": 0,
        "provider": "cpu",
        "model_type": "",
        "modeling_unit": "bpe" if asset.family == "transducer" else "",
    }

    if asset.family == "transducer":
        model_config["bpe_vocab"] = "bpe.vocab"
        model_config["bpe_model"] = "bpe.model"
        model_config["transducer"] = {
            "encoder": "encoder.int8.onnx" if asset.quantization == "int8" else "encoder.onnx",
            "decoder": "decoder.onnx",
            "joiner": "joiner.int8.onnx" if asset.quantization == "int8" else "joiner.onnx",
        }
    elif asset.family == "sense_voice":
        model_config["sense_voice"] = {
            "model": "model.int8.onnx" if asset.quantization == "int8" else "model.onnx",
            "language": asset.language,
            "use_itn": True,
        }
    elif asset.family == "moonshine":
        model_config["moonshine"] = {
            "preprocessor": "preprocess.onnx",
            "encoder": "encode.int8.onnx" if asset.quantization == "int8" else "encode.onnx",
            "uncached_decoder": "uncached_decode.int8.onnx" if asset.quantization == "int8" else "uncached_decode.onnx",
            "cached_decoder": "cached_decode.int8.onnx" if asset.quantization == "int8" else "cached_decode.onnx",
        }
    elif asset.family == "zipformer2_ctc":
        model_config["zipformer2_ctc"] = {
            "model": "model.int8.onnx" if asset.quantization == "int8" else "model.onnx"
        }
    elif asset.family == "qwen3_asr":
        model_config["tokens"] = ""
        model_config["qwen3_asr"] = {
            "conv_frontend": "conv_frontend.onnx",
            "encoder": "encoder.int8.onnx" if asset.quantization == "int8" else "encoder.onnx",
            "decoder": "decoder.int8.onnx" if asset.quantization == "int8" else "decoder.onnx",
            "tokenizer": "tokenizer",
            "max_total_len": 4096,
            "max_new_tokens": 1024,
            "temperature": 1.0,
            "top_p": 0.9,
            "seed": 0,
        }

    recognizer_config: Dict[str, Any] = {
        "feat_config": {
            "sample_rate": 16000,
            "feature_dim": 80,
        },
        "decoding_method": "greedy_search",
        "max_active_paths": 4,
        "hotwords_file": "",
        "hotwords_score": 1.5,
        "rule_fsts": "",
        "rule_fars": "",
        "blank_penalty": 0.0,
        "hr": {
            "lexicon": "",
            "rule_fsts": "",
        },
    }

    if asset.runtime == "online":
        recognizer_config["enable_endpoint"] = 0
        recognizer_config["rule1_min_trailing_silence"] = 2.4
        recognizer_config["rule2_min_trailing_silence"] = 1.2
        recognizer_config["rule3_min_utterance_length"] = 20.0

    return {
        "id": asset.suggested_id,
        "short_id": asset.suggested_short_id,
        "urls": urls,
        "sha256": "<CALCULATE_SHA256_ON_IMPORT>",
        "size_bytes": asset.size_bytes,
        "language": asset.language,
        "vinput_model": {
            "backend": backend,
            "language": asset.language,
            "size_bytes": asset.size_bytes,
            "supports_hotwords": asset.supports_hotwords,
            "runtime": asset.runtime,
            "family": asset.family,
            "recognizer": recognizer_config,
            "model": model_config,
        },
    }


def generate_markdown_report(
    diff_items: List[ModelDiffItem],
    local_models_count: int,
    upstream_repos: List[str],
    tags: List[str],
    since_date: str,
    total_scanned_count: int,
) -> str:
    """Generate clean Markdown report with zero emojis."""
    now_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    new_items = [d for d in diff_items if d.status == "new"]
    updated_items = [d for d in diff_items if d.status == "updated"]
    up_to_date_items = [d for d in diff_items if d.status == "up_to_date"]

    has_real_updates = bool(new_items or updated_items)

    changed_items = sorted(
        new_items + updated_items,
        key=lambda x: (x.asset.date_version or x.asset.created_at[:10], x.asset.size_bytes),
        reverse=True,
    )
    changed_by_size = sorted(new_items + updated_items, key=lambda x: x.asset.size_bytes)

    lines: List[str] = [
        "# 上游模型监控报告 (Upstream Models Monitor)",
        "",
        f"> 检查时间: `{now_str}` | 基准日期: `{since_date}` | 上游: `{', '.join(upstream_repos)}` (`{', '.join(tags)}`) | 平台: PC (x86/ARM)",
        "",
        "---",
        "",
        "## 检查结果",
        "",
    ]

    if not has_real_updates:
        lines.extend([
            f"**暂无新更新**: 自基准日期 `{since_date}` 以来，上游未发布新的 PC ASR 模型或现有模型更新构建。",
            f"本地收录模型（共 {local_models_count} 个）与上游保持同步。",
            "",
            "<details>",
            f"<summary>已收录模型状态 ({len(up_to_date_items)} 个)</summary>",
            "",
            "| 模型 ID | 当前收录文件 | 体积大小 | 状态 |",
            "| :--- | :--- | :---: | :---: |",
        ])
        for d in sorted(up_to_date_items, key=lambda x: x.asset.size_bytes):
            m_id = d.local_match.id if d.local_match else d.asset.name
            lines.append(f"| `{m_id}` | `{d.asset.name}` | {format_size(d.asset.size_bytes)} | 最新 |")
        lines.extend([
            "",
            "</details>",
            "",
        ])
        return "\n".join(lines)

    lines.extend([
        "| 类别 | 数量 |",
        "| :--- | :--- |",
        f"| 新增发布模型 | {len(new_items)} |",
        f"| 现有模型更新 | {len(updated_items)} |",
        "",
        "---",
        "",
        f"### 1. 更新时间线 (自 `{since_date}` 以来)",
        "",
        "| 发布日期 | 变动类型 | 模型文件名 / 标识 | 体积大小 | 语言 | 模式 / 架构 | 量化 | 资源链接 |",
        "| :---: | :---: | :--- | :---: | :---: | :--- | :---: | :--- |",
    ])
    for d in changed_items:
        a = d.asset
        date_str = a.date_version or (a.created_at[:10] if a.created_at else "-")
        status_badge = "全新发布" if d.status == "new" else "版本升级"
        name_col = f"`{a.name}`"
        if d.status == "updated" and d.local_match:
            local_ver = d.local_match.date_version or "本地旧版"
            name_col += f"<br><sub>(本地: `{local_ver}` -> 上游: `{a.date_version}`)</sub>"

        lines.append(
            f"| {date_str} | {status_badge} | {name_col} | {format_size(a.size_bytes)} | `{a.language}` | `{a.runtime}` / `{a.family}` | `{a.quantization}` | [下载]({a.download_url}) |"
        )
    lines.append("")

    lines.extend([
        "---",
        "",
        "### 2. 候选模型体积排序 (从小到大)",
        "",
        "| 模型文件名 | 体积大小 | 变动类型 | 语言 | 运行模式 | 模型结构 | 量化 | 下载链接 |",
        "| :--- | :---: | :---: | :---: | :--- | :---: | :---: | :--- |",
    ])
    for d in changed_by_size:
        a = d.asset
        status_text = "全新发布" if d.status == "new" else "版本升级"
        lines.append(
            f"| `{a.name}` | {format_size(a.size_bytes)} | {status_text} | `{a.language}` | `{a.runtime}` | `{a.family}` | `{a.quantization}` | [下载]({a.download_url}) |"
        )
    lines.append("")

    lines.extend([
        "---",
        "",
        "### 3. 引入说明",
        "",
        "1. **计算哈希**: 下载对应 `.tar.bz2` 并执行 `sha256sum <文件名>`",
        "2. **配置草稿**: 参考附件 `new_models_draft.json` 复制到 `registry/models.json`",
        "3. **补充文案**: 在 `i18n/zh_CN.json` 和 `i18n/en_US.json` 补充对应的 title 与 description",
        "",
    ])

    return "\n".join(lines)


def manage_tracking_issue(
    repo: str,
    token: str,
    title: str,
    body: str,
    has_updates: bool,
    since_date: str,
) -> None:
    """Create or update a tracking GitHub Issue via PATCH."""
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "vinput-registry-monitor",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    safe_body = body
    if len(safe_body) > 60000:
        safe_body = (
            safe_body[:59000]
            + "\n\n---\n> 提示: 候选模型条目较多，已自动截断。完整清单与配置草稿请查看 Action Run Summary 或下载 Artifacts 附件。"
        )

    # Find existing open tracking issue (by title or label)
    search_url = f"https://api.github.com/repos/{repo}/issues?state=open&per_page=50"
    req = urllib.request.Request(search_url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            issues = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"Warning: Failed to fetch existing issues: {e}", file=sys.stderr)
        issues = []

    existing_issue = None
    for iss in issues:
        if "pull_request" in iss:
            continue
        if iss.get("title") == title or "upstream-monitor" in [l.get("name") for l in iss.get("labels", [])]:
            existing_issue = iss
            break

    if existing_issue:
        issue_number = existing_issue["number"]
        print(f"Found existing tracking issue #{issue_number}. Updating content via PATCH...")
        update_url = f"https://api.github.com/repos/{repo}/issues/{issue_number}"
        payload = json.dumps({"title": title, "body": safe_body}).encode("utf-8")
        patch_req = urllib.request.Request(
            update_url,
            data=payload,
            headers={**headers, "Content-Type": "application/json"},
            method="PATCH",
        )
        with urllib.request.urlopen(patch_req, timeout=30) as resp:
            print(f"Successfully updated issue #{issue_number}.")
    else:
        print("Creating a new tracking issue...")
        create_url = f"https://api.github.com/repos/{repo}/issues"
        payload_data: Dict[str, Any] = {
            "title": title,
            "body": safe_body,
        }
        try:
            payload = json.dumps({**payload_data, "labels": ["upstream-monitor"]}).encode("utf-8")
            post_req = urllib.request.Request(
                create_url,
                data=payload,
                headers={**headers, "Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(post_req, timeout=30) as resp:
                created = json.loads(resp.read().decode("utf-8"))
                print(f"Successfully created fresh tracking issue #{created.get('number')}.")
        except urllib.error.HTTPError as e:
            if e.code == 422:
                print("Retrying issue creation without custom label...")
                payload = json.dumps(payload_data).encode("utf-8")
                post_req = urllib.request.Request(
                    create_url,
                    data=payload,
                    headers={**headers, "Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(post_req, timeout=30) as resp:
                    created = json.loads(resp.read().decode("utf-8"))
                    print(f"Successfully created fresh tracking issue #{created.get('number')}.")
            else:
                raise


def main() -> None:
    parser = argparse.ArgumentParser(description="Monitor upstream PC ASR models for vinput-registry.")
    parser.add_argument(
        "--registry",
        type=Path,
        default=Path("registry/models.json"),
        help="Path to local registry/models.json",
    )
    parser.add_argument(
        "--repo",
        type=str,
        default="k2-fsa/sherpa-onnx",
        help="Upstream GitHub repository (owner/repo)",
    )
    parser.add_argument(
        "--tags",
        type=str,
        default="asr-models",
        help="Release tag(s) to monitor (default: asr-models)",
    )
    parser.add_argument(
        "--since",
        type=str,
        default="today",
        help="Only report models released after this date (YYYY-MM-DD, 'today', or 'Nd'). Default: today",
    )
    parser.add_argument(
        "--token",
        type=str,
        default=os.environ.get("GITHUB_TOKEN"),
        help="GitHub API Token (optional)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("output_report"),
        help="Directory to save generated report and artifacts",
    )
    parser.add_argument(
        "--mock-file",
        type=Path,
        default=None,
        help="Path to mock JSON file containing release assets",
    )
    parser.add_argument(
        "--issue-title",
        type=str,
        default="Upstream Models Monitor",
        help="Title for tracking issue",
    )
    parser.add_argument(
        "--create-issue",
        action="store_true",
        help="Create or update a GitHub issue using GITHUB_TOKEN and GitHub API",
    )

    args = parser.parse_args()

    tags_list = [t.strip() for t in args.tags.split(",") if t.strip()]
    since_date = resolve_since_date(args.since)
    print(f"Monitoring updates strictly after baseline date: {since_date}")

    # 1. Load local models
    local_models = load_local_registry(args.registry)
    print(f"Loaded {len(local_models)} models from {args.registry}")

    # 2. Fetch upstream assets
    if args.mock_file and args.mock_file.exists():
        print(f"Loading mock upstream assets from {args.mock_file}")
        with open(args.mock_file, "r", encoding="utf-8") as f:
            raw_assets = json.load(f)
        upstream_assets = []
        for a in raw_assets:
            if not is_pc_compatible(a["name"]):
                continue
            meta = infer_model_metadata(a["name"], a.get("size", 0))
            upstream_assets.append(
                UpstreamAsset(
                    name=a["name"],
                    download_url=a.get("browser_download_url", ""),
                    size_bytes=a.get("size", 0),
                    created_at=a.get("created_at", ""),
                    updated_at=a.get("updated_at", ""),
                    release_tag=a.get("release_tag", "asr-models"),
                    repo=args.repo,
                    base_name=meta["base_name"],
                    date_version=meta["date_version"],
                    extension=meta["extension"],
                    language=meta["language"],
                    runtime=meta["runtime"],
                    family=meta["family"],
                    quantization=meta["quantization"],
                    supports_hotwords=meta["supports_hotwords"],
                    suggested_id=meta["suggested_id"],
                    suggested_short_id=meta["suggested_short_id"],
                )
            )
    else:
        upstream_assets = fetch_upstream_assets(
            repo=args.repo,
            tags=tags_list,
            token=args.token,
        )

    print(f"Fetched {len(upstream_assets)} PC-compatible upstream assets.")

    # 3. Compare models with since_date filter
    diff_items = compare_models(local_models, upstream_assets, since_date=since_date)

    new_count = sum(1 for d in diff_items if d.status == "new")
    updated_count = sum(1 for d in diff_items if d.status == "updated")
    up_to_date_count = sum(1 for d in diff_items if d.status == "up_to_date")

    print(
        f"Analysis complete (since {since_date}): {new_count} new, {updated_count} updated, {up_to_date_count} up-to-date."
    )

    # 4. Generate outputs
    args.output_dir.mkdir(parents=True, exist_ok=True)

    # Markdown report
    md_report = generate_markdown_report(
        diff_items=diff_items,
        local_models_count=len(local_models),
        upstream_repos=[args.repo],
        tags=tags_list,
        since_date=since_date,
        total_scanned_count=len(upstream_assets),
    )
    report_md_path = args.output_dir / "summary.md"
    with open(report_md_path, "w", encoding="utf-8") as f:
        f.write(md_report)
    print(f"Written Markdown report to {report_md_path}")

    # JSON summary
    sorted_by_size = sorted(diff_items, key=lambda d: d.asset.size_bytes)
    sorted_by_date = sorted(diff_items, key=lambda d: (d.asset.date_version or d.asset.created_at[:10]), reverse=True)

    json_summary = {
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "baseline_since_date": since_date,
        "upstream_repo": args.repo,
        "tags": tags_list,
        "platform": "pc_x86_arm",
        "counts": {
            "local_models": len(local_models),
            "upstream_assets_scanned": len(upstream_assets),
            "new_models_since_baseline": new_count,
            "updated_models_since_baseline": updated_count,
        },
        "recent_changes": [
            {
                "status": d.status,
                "asset": asdict(d.asset),
                "local_match": asdict(d.local_match) if d.local_match else None,
                "reason": d.reason,
            }
            for d in sorted_by_date
            if d.status in ("new", "updated")
        ],
    }
    report_json_path = args.output_dir / "summary.json"
    with open(report_json_path, "w", encoding="utf-8") as f:
        json.dump(json_summary, f, indent=2, ensure_ascii=False)
    print(f"Written JSON summary to {report_json_path}")

    # Draft configurations for new models
    new_drafts = [build_draft_model_json(d.asset) for d in sorted_by_size if d.status == "new"]
    drafts_path = args.output_dir / "new_models_draft.json"
    with open(drafts_path, "w", encoding="utf-8") as f:
        json.dump(new_drafts, f, indent=2, ensure_ascii=False)
    print(f"Written {len(new_drafts)} draft model configs to {drafts_path}")

    # Set GitHub Actions output
    gh_output = os.environ.get("GITHUB_OUTPUT")
    if gh_output:
        with open(gh_output, "a", encoding="utf-8") as f:
            f.write(f"has_updates={'true' if (new_count > 0 or updated_count > 0) else 'false'}\n")
            f.write(f"new_count={new_count}\n")
            f.write(f"updated_count={updated_count}\n")

    # Set Step Summary
    gh_step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if gh_step_summary:
        with open(gh_step_summary, "a", encoding="utf-8") as f:
            f.write(md_report)
            f.write("\n")

    # Handle GitHub Issue creation/update
    if args.create_issue and args.token and os.environ.get("GITHUB_REPOSITORY"):
        current_repo = os.environ.get("GITHUB_REPOSITORY")
        print(f"Managing tracking issue on {current_repo}...")
        try:
            manage_tracking_issue(
                repo=current_repo,
                token=args.token,
                title=args.issue_title,
                body=md_report,
                has_updates=(new_count > 0 or updated_count > 0),
                since_date=since_date,
            )
        except Exception as e:
            print(f"Warning: Failed to manage tracking issue: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
