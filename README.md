# vinput-registry

Registry repository for vinput resources.

## Layout

- `registry/`: top-level resource indexes
- `i18n/`: resource display text, one file per language
- `resources/providers/<folder>/<name>/`: cloud ASR provider scripts
- `resources/adapters/<folder>/<name>/`: managed local adaptor scripts

## Resource Rules

- Resource ids are stable machine identifiers.
- Resource ids should follow `<kinds>.<folder>.<name>`.
- `short_id` is required for every resource item.
- `short_id` is only for human interaction in CLI/GUI/logs.
- `short_id` must not be used for storage paths, download paths, or internal
  resource resolution.
- Download-related URLs use array form for fallback
- Resource display text is stored in i18n files as:
  - `<id>.title`
  - `<id>.description`
- Script resources should keep:
  - `entry.py`
  - `README.md`

## Resource Fields

All resource items should contain:

- `id`: stable machine identifier
- `short_id`: short human-facing identifier

Provider items additionally contain:

- `stream`: `true` for streaming scripts, `false` for one-shot scripts
- `envs`: runtime environment variable declarations for local execution

## Provider Env Rules

Provider env names should use the `VINPUT_ASR_*` namespace.

Shared provider envs should keep stable names and meanings:

- `VINPUT_ASR_API_KEY`: bearer-style API credential
- `VINPUT_ASR_APP_ID`: app identifier for providers that require it separately
- `VINPUT_ASR_ACCESS_TOKEN`: token credential for providers that do not use API keys
- `VINPUT_ASR_URL`: full request or websocket endpoint override
- `VINPUT_ASR_BASE_URL`: base endpoint override when the script constructs the final path
- `VINPUT_ASR_MODEL`: remote model identifier
- `VINPUT_ASR_LANGUAGE`: explicit transcription language hint
- `VINPUT_ASR_PROMPT`: optional recognition prompt or bias text
- `VINPUT_ASR_TIMEOUT`: end-to-end network timeout in seconds
- `VINPUT_ASR_FINISH_GRACE_SECS`: extra wait time after local `finish` before closing
- `VINPUT_ASR_ENABLE_VAD`: enable server-side VAD when the provider supports it
- `VINPUT_ASR_VAD_THRESHOLD`: provider VAD sensitivity threshold
- `VINPUT_ASR_VAD_PREFIX_PADDING_MS`: audio padding kept before detected speech
- `VINPUT_ASR_VAD_SILENCE_DURATION_MS`: silence duration used to close a speech turn

Provider-specific envs are allowed when the upstream API exposes features that do
not map cleanly to the shared set. These should still use the `VINPUT_ASR_*`
prefix and should be listed after the shared envs in `registry/providers.json`.

## Current Scope

- `models.json`: local ASR models
- `providers.json`: cloud ASR provider scripts
- `adapters.json`: managed local LLM adapter scripts

## `vinput_model` Rules

For local ASR models, `vinput_model` is runtime-facing metadata and should use
the following top-level shape:

- `backend`: local backend selector such as `sherpa-offline` or
  `sherpa-streaming`
- `runtime`: `online` or `offline`
- `family`: `sherpa-onnx` C API family name
- `recognizer`: recognizer config fields
- `model`: model config fields

Field naming should stay as close as possible to `sherpa-onnx` C API naming.

## Terminology

- `models`: local ASR model assets
- `providers`: cloud ASR service scripts
- `adapters`: managed local LLM adaptor scripts
- `backend`: local ASR engine/backend selector for model runtime metadata
