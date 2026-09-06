# adapters.cjk-trim.proxy

Managed local LLM adapter that removes redundant spaces around CJK punctuation and between CJK ideographs in ASR transcripts via a local OpenAI-compatible proxy.

Some speech recognition models (e.g. x-asr Zipformer Transducer) insert spurious spaces into Chinese output. This adapter removes them locally as a zero-latency, offline service (`POST /v1/chat/completions`) for `vinput-daemon`.

**Scope note**: ASCII / English text is intentionally left untouched — no ASCII punctuation normalization and no interior space collapsing. Only leading/trailing whitespace is stripped (`strip()`).

## Entry

- `entry.py`

## Runtime

- command: `python3`
- endpoints:
  - `POST /v1/chat/completions` (extracts input from `<vinput-asr>` tags or user messages, returns `{"candidates": [cleaned_text]}`)
  - `GET /v1/models` (returns `cjk-trim` model definition)
- dependencies: Python 3 standard library only (`http.server`, `json`, `os`, `re`, `sys`, `time`, `uuid`)

## Environment Variables

### Required
- `CJK_TRIM_PORT` (required): Local HTTP listening port (e.g. `9090`). The script requires an explicit port and will exit with code 2 if omitted or invalid.

### Optional
- `CJK_TRIM_COLLAPSE_CJK_SPACES` (optional, default: `true`): Whether to collapse spaces between consecutive CJK ideographs (`\u4e00-\u9fff`, `\u3400-\u4dbf`, `\uf900-\ufaff`). Kept out of `registry/adapters.json` on purpose (minimally viable env set); document-level tuneable only.

## Unicode Punctuation Ranges Used

1. `\u3000-\u303F`: CJK Symbols and Punctuation (e.g. `。`, `、`, `《`, `》`)
2. `\uFF01-\uFF65`: Fullwidth Forms (e.g. `，`, `！`, `？`, `：`, `；`)
3. `\u2000-\u206F`: General Punctuation (e.g. `——`, `……`, quotes)
4. `\uFE10-\uFE1F`: Presentation Forms for Vertical Lines
5. `\uFE30-\uFE4F`: CJK Compatibility Forms

## Example Transformations

### Chinese Sample (CJK Fullwidth Punctuation)
- **Before**: `嗯， 这句话就是我实际语音发出来的文字， 它的空格是这样的， 基本上是在一个标点的后面。`
- **After**: `嗯，这句话就是我实际语音发出来的文字，它的空格是这样的，基本上是在一个标点的后面。`

### CJK Ideograph Spacing
- **Before**: `这 是 一 个 测 试`
- **After**: `这是一个测试`

### English Sample (untouched by design)
- **Before**: `Uh, hello, this is English test. Maybe they have this issue to.`
- **After**: `Uh, hello, this is English test. Maybe they have this issue to.`

### Mixed Sample
- **Before**: `你好，  世界 。 test`
- **After**: `你好，世界。test`
