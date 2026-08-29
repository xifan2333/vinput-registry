#!/usr/bin/env python3
"""
Upstream Model Monitor for vinput-registry.

Monitors upstream model releases (e.g. k2-fsa/sherpa-onnx tag: asr-models)
for PC-compatible (x86 & ARM CPU) ASR models.
Provides dual-perspective reporting:
1. Timeline view: Recent updates & newly released models (sorted by date descending)
2. Size Tiers view: Capacity tiers <100MB, 100-300MB, >300MB (sorted by size ascending)

Produces Markdown reports, JSON summaries, and draft configurations without
modifying any repository code.
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


def compare_models(
    local_models: List[LocalModel],
    upstream_assets: List[UpstreamAsset],
) -> List[ModelDiffItem]:
    """Compare upstream assets against local models."""
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
        # 1. Exact filename match (same version date)
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
                    status = "updated"
                elif asset.date_version < matched_local.date_version:
                    reason = (
                        f"Upstream asset date ({asset.date_version}) is older than "
                        f"local ({matched_local.date_version})."
                    )
                    status = "up_to_date"
                else:
                    reason = f"Same date version ({asset.date_version}), but filename/hash differs."
                    status = "updated"
            else:
                reason = f"Matching model base found ({matched_local.id}), candidate update."
                status = "updated"

            diff_items.append(
                ModelDiffItem(
                    asset=asset,
                    local_match=matched_local,
                    status=status,
                    reason=reason,
                )
            )
        else:
            # 3. Completely new model
            diff_items.append(
                ModelDiffItem(
                    asset=asset,
                    local_match=None,
                    status="new",
                    reason="New model asset not present in local registry.",
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
) -> str:
    """
    Generate comprehensive dual-perspective Markdown report:
    1. Timeline of changes (sorted by release date descending)
    2. Size tiers (<100MB, 100-300MB, >300MB, sorted by size ascending)
    """
    now_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    new_items = [d for d in diff_items if d.status == "new"]
    updated_items = [d for d in diff_items if d.status == "updated"]
    up_to_date_items = [d for d in diff_items if d.status == "up_to_date"]

    # --- View 1: Timeline changes (sorted by created_at or date_version descending) ---
    changed_items = sorted(
        new_items + updated_items,
        key=lambda x: (x.asset.date_version or x.asset.created_at[:10], x.asset.size_bytes),
        reverse=True,
    )

    # --- View 2: Size Tiers ---
    # Tier 1: Ultra Lightweight (< 100 MB)
    tier_small = sorted(
        [d for d in diff_items if d.asset.size_bytes < 100 * 1024 * 1024],
        key=lambda x: x.asset.size_bytes,
    )
    # Tier 2: Balanced Standard (100 MB ~ 300 MB)
    tier_mid = sorted(
        [d for d in diff_items if 100 * 1024 * 1024 <= d.asset.size_bytes <= 300 * 1024 * 1024],
        key=lambda x: x.asset.size_bytes,
    )
    # Tier 3: Large / High Precision (> 300 MB)
    tier_large = sorted(
        [d for d in diff_items if d.asset.size_bytes > 300 * 1024 * 1024],
        key=lambda x: x.asset.size_bytes,
    )

    lines: List[str] = [
        "# 🔍 上游 PC 模型更新与选型参考 (Upstream PC Models Monitor)",
        "",
        f"> 🕒 **检查时间**: `{now_str}`  ",
        f"> 📦 **监控源**: `{', '.join(upstream_repos)}` (Tag: `{', '.join(tags)}`)  ",
        f"> 💻 **运行平台**: 标准 PC (x86 & ARM CPU 支持)  ",
        f"> 📚 **本地已收录**: `{local_models_count}` 个模型 | 🆕 **未收录新模型**: `{len(new_items)}` | 🔄 **现有更新**: `{len(updated_items)}`  ",
        f"> ℹ️ **说明**: 本任务仅提供参考对比与选型指引，**不改动当前项目仓库代码**。",
        "",
        "---",
        "",
        "## 📊 概览统计 (Overview)",
        "",
        "| 类别 | 数量 | 状态速览 |",
        "| :--- | :--- | :--- |",
        f"| 🆕 **未收录新模型** | **{len(new_items)}** | {'🔔 发现未收录新模型' if new_items else '✅ 无新模型'} |",
        f"| 🔄 **现有模型新版本** | **{len(updated_items)}** | {'⚡ 发现版本更新' if updated_items else '✅ 已是最新'} |",
        f"| ✅ **已收录且最新** | **{len(up_to_date_items)}** | 正常保持 |",
        "",
    ]

    # ==========================================
    # 视角一：最新发布与更新时间线 (看更新)
    # ==========================================
    lines.extend([
        "---",
        "",
        "## ⏱️ 视角一：最新变动动态 (按发布时间从新到旧，看最新更新)",
        "> 第一时间呈现上游最新的变动：包括发布了哪些新模型、已有模型是否有新构建版本。",
        "",
    ])

    if changed_items:
        lines.extend([
            "| 发布日期 | 变动类型 | 模型文件名 / 标识 | 体积大小 | 语言 | 模式 / 架构 | 量化 | 资源链接 |",
            "| :---: | :---: | :--- | :---: | :---: | :--- | :---: | :--- |",
        ])
        for d in changed_items:
            a = d.asset
            date_str = a.date_version or (a.created_at[:10] if a.created_at else "-")
            status_badge = "🆕 **全新模型**" if d.status == "new" else "🔄 **版本升级**"
            name_col = f"`{a.name}`"
            if d.status == "updated" and d.local_match:
                local_ver = d.local_match.date_version or "本地旧版"
                name_col += f"<br><sub>(本地: `{local_ver}` ➔ 上游: `{a.date_version}`)</sub>"

            lines.append(
                f"| **{date_str}** | {status_badge} | {name_col} | **{format_size(a.size_bytes)}** | `{a.language}` | `{a.runtime}` / `{a.family}` | `{a.quantization}` | [🔗 下载链接]({a.download_url}) |"
            )
        lines.append("")
    else:
        lines.extend([
            "_✅ 近期暂无未收录的新模型或版本更新，本地库与上游完全同步。_",
            "",
        ])

    # ==========================================
    # 视角二：按体积梯队选型表 (看大小)
    # ==========================================
    lines.extend([
        "---",
        "",
        "## 📦 视角二：体积梯队选型清单 (按体积从小到大，看规格选型)",
        "> 划分容量梯队，各梯队内按体积升序排列，方便根据桌面端内存与性能要求挑选模型。",
        "",
    ])

    def render_tier_table(items: List[ModelDiffItem]) -> List[str]:
        if not items:
            return ["_该梯队暂无模型_", ""]
        t_lines = [
            "| 模型文件名 | 体积大小 | 状态 | 语言 | 模式 | 架构 | 量化 | 发布日期 | 下载链接 |",
            "| :--- | :---: | :---: | :---: | :---: | :--- | :---: | :---: | :--- |",
        ]
        for d in items:
            a = d.asset
            status_icon = "🆕 未收录" if d.status == "new" else ("🔄 有新版" if d.status == "updated" else "✅ 已收录")
            date_str = a.date_version or (a.created_at[:10] if a.created_at else "-")
            t_lines.append(
                f"| `{a.name}` | **{format_size(a.size_bytes)}** | {status_icon} | `{a.language}` | `{a.runtime}` | `{a.family}` | `{a.quantization}` | {date_str} | [🔗 下载]({a.download_url}) |"
            )
        t_lines.append("")
        return t_lines

    # Tier 1
    lines.extend([
        f"### ⚡ 梯队一：超轻量级 `< 100 MB` ({len(tier_small)} 个)",
        "> 极致低资源占用，适合秒开、极低内存消耗的日常输入场景。",
        "",
    ])
    lines.extend(render_tier_table(tier_small))

    # Tier 2
    lines.extend([
        f"### ⚖️ 梯队二：标准平衡级 `100 MB ~ 300 MB` ({len(tier_mid)} 个)",
        "> 兼顾识别准确率与响应速度，主流桌面 ASR 首选梯队（包含 SenseVoice / Moonshine / Zipformer 等）。",
        "",
    ])
    lines.extend(render_tier_table(tier_mid))

    # Tier 3
    lines.extend([
        f"### 🎯 梯队三：高精度 / 大参数级 `> 300 MB` ({len(tier_large)} 个)",
        "> 强抗噪、多语言转写、专业长文本识别，适合具备充足内存的 PC 设备（包含 Qwen3-ASR / FireRedASR 等）。",
        "",
    ])
    lines.extend(render_tier_table(tier_large))

    # Next steps
    lines.extend([
        "---",
        "",
        "## 💡 如何将选中的模型引入本地？",
        "",
        "1. **选型评估**: 先在【视角一】看是否有近期更新，或在【视角二】按体积梯队选定目标模型。",
        "2. **下载与哈希计算**: 下载对应 `.tar.bz2` 并执行 `sha256sum <文件名>` 获取哈希校验值。",
        "3. **应用配置草稿**: 直接在 Workflow Artifacts 中的 `new_models_draft.json` 复制预生成的 JSON 块粘贴到 `registry/models.json`。",
        "4. **添加国际化文案**: 在 `i18n/zh_CN.json` 和 `i18n/en_US.json` 补齐 `<id>.title` 与 `<id>.description`。",
        "",
    ])

    return "\n".join(lines)


def manage_tracking_issue(
    repo: str,
    token: str,
    title: str,
    body: str,
    has_updates: bool,
) -> None:
    """Create or update a tracking GitHub Issue."""
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "vinput-registry-monitor",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    # GitHub issue body has a limit of 65,536 characters
    safe_body = body
    if len(safe_body) > 60000:
        safe_body = (
            safe_body[:59000]
            + "\n\n---\n> ⚠️ **提示**: 候选模型条目较多，Issue 正文已自动截断以符合 GitHub 长度限制。完整清单及配置草稿（Draft JSON）请查看当前 Action 的 **Run Summary** 或下载 **Artifacts** 附件。"
        )

    # Find existing open issue with the same title or label
    search_url = f"https://api.github.com/repos/{repo}/issues?state=open&per_page=50"
    req = urllib.request.Request(search_url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        issues = json.loads(resp.read().decode("utf-8"))

    existing_issue = None
    for iss in issues:
        if "pull_request" in iss:
            continue
        if iss.get("title") == title or "upstream-monitor" in [l.get("name") for l in iss.get("labels", [])]:
            existing_issue = iss
            break

    if existing_issue:
        issue_number = existing_issue["number"]
        print(f"Found existing tracking issue #{issue_number}. Updating content...")
        update_url = f"https://api.github.com/repos/{repo}/issues/{issue_number}"
        payload = json.dumps({"body": safe_body}).encode("utf-8")
        patch_req = urllib.request.Request(
            update_url,
            data=payload,
            headers={**headers, "Content-Type": "application/json"},
            method="PATCH",
        )
        with urllib.request.urlopen(patch_req, timeout=30) as resp:
            print(f"Successfully updated issue #{issue_number}.")
    else:
        if has_updates:
            print("No existing tracking issue found. Creating a new one...")
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
                    print(f"Successfully created tracking issue #{created.get('number')}.")
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
                        print(f"Successfully created tracking issue #{created.get('number')}.")
                else:
                    raise
        else:
            print("No updates detected and no existing issue. Skipping issue creation.")


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
        "--token",
        type=str,
        default=os.environ.get("GITHUB_TOKEN"),
        help="GitHub API Token (optional, avoids rate limits)",
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
        help="Path to mock JSON file containing release assets (for offline testing)",
    )
    parser.add_argument(
        "--issue-title",
        type=str,
        default="🤖 上游 PC 模型更新监控报告 (Upstream Models Monitor)",
        help="Title for tracking issue",
    )
    parser.add_argument(
        "--create-issue",
        action="store_true",
        help="Create or update a GitHub issue using GITHUB_TOKEN and GitHub API",
    )

    args = parser.parse_args()

    tags_list = [t.strip() for t in args.tags.split(",") if t.strip()]

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

    # 3. Compare models
    diff_items = compare_models(local_models, upstream_assets)

    new_count = sum(1 for d in diff_items if d.status == "new")
    updated_count = sum(1 for d in diff_items if d.status == "updated")
    up_to_date_count = sum(1 for d in diff_items if d.status == "up_to_date")

    print(f"Analysis complete: {new_count} new, {updated_count} updated, {up_to_date_count} up-to-date.")

    # 4. Generate outputs
    args.output_dir.mkdir(parents=True, exist_ok=True)

    # Markdown report (Dual view)
    md_report = generate_markdown_report(
        diff_items=diff_items,
        local_models_count=len(local_models),
        upstream_repos=[args.repo],
        tags=tags_list,
    )
    report_md_path = args.output_dir / "summary.md"
    with open(report_md_path, "w", encoding="utf-8") as f:
        f.write(md_report)
    print(f"Written Markdown report to {report_md_path}")

    # JSON summary (Dual structured index: by date & by size)
    sorted_by_size = sorted(diff_items, key=lambda d: d.asset.size_bytes)
    sorted_by_date = sorted(diff_items, key=lambda d: (d.asset.date_version or d.asset.created_at[:10]), reverse=True)

    json_summary = {
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "upstream_repo": args.repo,
        "tags": tags_list,
        "platform": "pc_x86_arm",
        "counts": {
            "local_models": len(local_models),
            "upstream_assets": len(upstream_assets),
            "new_models": new_count,
            "updated_models": updated_count,
            "up_to_date_models": up_to_date_count,
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
        "size_tiers": {
            "under_100mb": [asdict(d.asset) for d in sorted_by_size if d.asset.size_bytes < 100 * 1024 * 1024],
            "100mb_to_300mb": [
                asdict(d.asset)
                for d in sorted_by_size
                if 100 * 1024 * 1024 <= d.asset.size_bytes <= 300 * 1024 * 1024
            ],
            "over_300mb": [asdict(d.asset) for d in sorted_by_size if d.asset.size_bytes > 300 * 1024 * 1024],
        },
    }
    report_json_path = args.output_dir / "summary.json"
    with open(report_json_path, "w", encoding="utf-8") as f:
        json.dump(json_summary, f, indent=2, ensure_ascii=False)
    print(f"Written JSON summary to {report_json_path}")

    # Draft configurations for new models (sorted by size ascending)
    new_drafts = [build_draft_model_json(d.asset) for d in sorted_by_size if d.status == "new"]
    drafts_path = args.output_dir / "new_models_draft.json"
    with open(drafts_path, "w", encoding="utf-8") as f:
        json.dump(new_drafts, f, indent=2, ensure_ascii=False)
    print(f"Written {len(new_drafts)} draft model configs to {drafts_path}")

    # Set GitHub Actions output if running in GH Actions
    gh_output = os.environ.get("GITHUB_OUTPUT")
    if gh_output:
        with open(gh_output, "a", encoding="utf-8") as f:
            f.write(f"has_updates={'true' if (new_count > 0 or updated_count > 0) else 'false'}\n")
            f.write(f"new_count={new_count}\n")
            f.write(f"updated_count={updated_count}\n")
            f.write(f"up_to_date_count={up_to_date_count}\n")

    # Set Step Summary if running in GH Actions
    gh_step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if gh_step_summary:
        with open(gh_step_summary, "a", encoding="utf-8") as f:
            f.write(md_report)
            f.write("\n")

    # Handle GitHub Issue creation/update if requested
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
            )
        except Exception as e:
            print(f"Warning: Failed to manage tracking issue: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
