# Resource Schemas & Model Specification Guide

This document defines schema standards for local ASR models, cloud ASR providers, and LLM scene adapters in `vinput-registry`.

---

## 1. Universal Resource Identifiers

Every catalog item in `registry/*.json` requires:

1. **`id`**: Stable machine identifier. First segment is kind, subsequent segments map to relative directory hierarchy:
   - Adapters: `adapter.<name>` $\to$ `resources/adapters/<name>/`
   - Providers: `provider.<vendor>.<name>` $\to$ `resources/providers/<vendor>/<name>/`
   - Models: `model.<source>.<model_name>` (e.g., `model.sherpa-onnx.<name>`)
   - Triggers: `trigger.<vendor>.<name>` $\to$ `resources/triggers/<vendor>/<name>/`
   - Receivers: `receiver.<name>` $\to$ `resources/receivers/<name>/`
2. **`short_id`**: Concise identifier for CLI/GUI display. Never used for internal file lookup.
3. **`script_urls` / `urls`**: Array with at least 3 fallback sources in order:
   - Primary GitHub raw URL: `https://raw.githubusercontent.com/xifan2333/vinput-registry/main/...`
   - Accelerator 1: `https://gh-proxy.com/https://raw.githubusercontent.com/xifan2333/vinput-registry/main/...`
   - Accelerator 2: `https://ghfast.top/https://raw.githubusercontent.com/xifan2333/vinput-registry/main/...`

---

## 2. Trigger & Receiver Scripts (`registry/triggers.json` & `registry/receivers.json`)

### Rules
1. **`id`**: Must be `trigger.<folder>.<name>` or `receiver.<folder>.<name>`.
2. **Lifecycle**: Long-lived child process managed via `ProcessSession`.
3. **Environment Variable Namespace**:
   - Trigger variables **must** begin with `VINPUT_TRIGGER_` (e.g., `VINPUT_TRIGGER_DEVICE_PATH`, `VINPUT_TRIGGER_CAPTURE_DEVICE`).
   - Receiver variables **must** begin with `VINPUT_RECEIVER_*`.
4. **Dependencies**: Scripts must rely exclusively on the Python 3 standard library (zero pip dependencies).
5. **Protocol**: Both kinds share the identical client pipe protocol (see `client-protocol.md`); the registry type expresses primary role only.

---

## 3. Local ASR Models Specification (`registry/models.json`)

When a model archive is downloaded and extracted, `fcitx5-vinput` flattens the directory and **writes `vinput_model` out as `vinput-model.json`**. The daemon strictly validates this file before initializing the recognizer.

### Top-Level Model Item Schema
```json
{
  "id": "model.sherpa-onnx.x-asr-zipformer-transducer-zh-en-punct-int8",
  "short_id": "onnx-xasr-zh-en-punct-int8-off",
  "urls": ["<primary>", "<mirror1>", "<mirror2>"],
  "sha256": "5d02c36d7b44e886b7c8f0d8e051f8713acab96c264bb6ef9e718be39a6a2224",
  "size_bytes": 136396739,
  "language": "zh_en",
  "vinput_model": { ... }
}
```

### `vinput_model` Specification (Aligning with sherpa-onnx C API)
```json
{
  "backend": "sherpa-offline",
  "language": "zh_en",
  "size_bytes": 136396739,
  "supports_hotwords": true,
  "runtime": "offline",
  "family": "transducer",
  "recognizer": {
    "feat_config": {
      "sample_rate": 16000,
      "feature_dim": 80
    },
    "decoding_method": "modified_beam_search",
    "max_active_paths": 4,
    "hotwords_score": 1.5
  },
  "model": {
    "tokens": "tokens.txt",
    "num_threads": 1,
    "provider": "cpu",
    "debug": 0,
    "modeling_unit": "bpe",
    "bpe_vocab": "bpe.vocab",
    "bpe_model": "bpe.model",
    "transducer": {
      "encoder": "encoder-epoch-99-avg-1.int8.onnx",
      "decoder": "decoder-epoch-99-avg-1.onnx",
      "joiner": "joiner-epoch-99-avg-1.int8.onnx"
    }
  }
}
```

#### Field Rules for `vinput_model`
- **`backend` vs `runtime`**:
  - `sherpa-offline` must pair with `runtime: "offline"`.
  - `sherpa-streaming` must pair with `runtime: "online"`.
- **`family`**: Supported architectures:
  - `transducer` (Zipformer Transducer)
  - `sense_voice` (SenseVoice Small)
  - `qwen3_asr` (Qwen3 ASR)
  - `zipformer2_ctc` (Zipformer CTC)
  - `moonshine` (Moonshine Tiny/Base)
  - `paraformer`, `whisper`, `nemo_ctc`, `telespeech_ctc`
- **Text Assets**:
  - Most families require `model.tokens` (e.g. `"tokens": "tokens.txt"`).
  - `qwen3_asr` and `funasr_nano` require `model.tokenizer` (e.g. `"tokenizer": "tokenizer.json"`).
- **Hotword Assets (`supports_hotwords: true`)**:
  - If `family` is `transducer` and `supports_hotwords` is true:
    - `model.modeling_unit` must be set (`bpe`, `bbpe`, `cjkchar`, or `cjkchar+bpe`).
    - If using BPE, `model.bpe_vocab` (e.g. `"bpe.vocab"`) and `model.bpe_model` must be specified.

---

## 4. Cloud ASR Providers (`registry/providers.json`)

### Rules
1. **`stream` property**:
   - `true`: Streaming duplex protocol.
   - `false`: One-shot batch protocol.
2. **Environment Variable Namespace**:
   - All provider environment variables **must** begin with `VINPUT_ASR_`.
   - Standard variables: `VINPUT_ASR_API_KEY`, `VINPUT_ASR_MODEL`, `VINPUT_ASR_LANGUAGE`, `VINPUT_ASR_URL`, `VINPUT_ASR_WORKSPACE_ID`.

---

## 5. LLM Scene Adapters (`registry/adapters.json`) & The Minimally Viable Env Set Principle

### 1. Unix Command Pipe Filter Architecture
Unlike daemon background services, LLM Scene Adapters are executed on-demand by `vinput-daemon` as Unix pipe filters (`RewriteWithCommandAdapter`):
- `stdin`: Daemon pipes JSON request containing text to be rewritten.
- `stdout`: Adapter returns rewritten JSON response (`{"type": "text_rewritten", "text": "..."}`).
- `stderr`: Dedicated to user desktop notifications (`{"title": "...", "body": "..."}`). 100% silent during normal operation.
- **No Ports Needed**: Adapters do not run as long-lived HTTP servers and do not expose or require any `*_PORT` environment variables.

### 2. The Minimally Viable Env Set Principle
When a user materializes/installs an adapter, `fcitx5-vinput` executes:
```cpp
FillDefaultEnvMap(entry.envs, &adapter.env); // Injects every declared env as "" into ~/.config/vinput/config.json
```

**Key Architectural Rule**:
- `registry/adapters.json` should declare **only the bare minimum variables required to run the adapter**:
  - **Required variables** (`"required": true`): Variables without which the script cannot function (e.g. `API_KEY`).
  - **Essential tuneables** (`"required": false`): Only critical user knobs (e.g., custom upstream URL).
- Do **not** declare fine-tuning options (timeouts, retry counts, logging levels) in `adapters.json` if they have robust default values in `entry.py`. Doing so clutters the user's `config.json` with empty placeholder strings and creates user confusion.

### 3. Three-Tier Parameter Architecture
1. **Tier 1: Registry (`adapters.json`)**: Minimally viable set (`required: true` + essential knobs).
2. **Tier 2: Script (`entry.py`)**: Fallback defaults via `os.getenv("PARAM", DEFAULT_VALUE)`.
3. **Tier 3: Documentation (`README.md`)**: Full documentation separating Required vs Optional settings.

### 4. Stderr Silence & Desktop Notification Coupling
`vinput-daemon` monitors the adapter child process's `stderr` via `FlushAdapterBuffer`:
```cpp
void FlushAdapterBuffer(ManagedAdapter& adapter, bool flush_partial) {
  if (!line.empty()) {
    EmitNotification(line); // Forwards every non-empty line to desktop notification via D-Bus!
  }
}
```
- **Every non-empty line emitted to `stderr` results in an immediate desktop popup notification for the user.**
- **Absolute Silence in Normal Operation**: Never print startup banners, options, or progress lines to `stderr`.
- **Errors Only**: Use `notify_error()` to write JSONL desktop notifications (`{"title": "...", "body": "..."}`) to `stderr` on genuine failures.

### 5. Adapter `README.md` Documentation Template
Each `resources/adapters/<name>/README.md` must clearly document runtime requirements:

```markdown
# adapters.<group>.<name>

Description of adapter purpose.

## Entry
- `entry.py`

## Runtime
- command: `python3`
- architecture: Unix command pipe filter (`RewriteWithCommandAdapter`)
- dependencies: Python 3 standard library only

## Environment Variables

### Required
- `TARGET_API_KEY` (required): Authentication credential.

### Optional
- `TARGET_URL` (optional, default: `http://localhost:8080`): Custom upstream endpoint.
```

---

## 6. i18n Catalogs (`i18n/en_US.json` & `i18n/zh_CN.json`)

Every registered item (`provider.*`, `model.*`, `adapter.*`) must have complete entries in both languages:
```json
{
  "<id>.title": "Human readable name",
  "<id>.description": "Concise summary of capabilities"
}
```
Run `mise run lint` (which calls `scripts/validate_registry.py`) to verify parity.
