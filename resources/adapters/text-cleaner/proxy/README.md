# adapters.text-cleaner.proxy

Managed local LLM adapter that normalizes spacing around punctuation and CJK characters in ASR transcripts.

Operates as a zero-latency, offline OpenAI-compatible proxy (`POST /v1/chat/completions`) for `vinput-daemon`. It removes redundant spaces emitted by speech recognition models (such as x-asr Zipformer Transducer) around punctuation marks matching standard Unicode codepoint ranges (without hardcoding specific punctuation characters).

## Entry

- `entry.py`

## Runtime

- command: `python3`
- endpoints:
  - `POST /v1/chat/completions` (extracts input from `<vinput-asr>` tags or user messages, returns `{"candidates": [cleaned_text]}`)
  - `GET /v1/models` (returns `text-cleaner` model definition)
- dependencies: Python 3 standard library only (`http.server`, `json`, `re`, `uuid`, `time`)

## Environment Variables

### Required
None. Runs out of the box with zero configuration.

### Optional
- `CLEANER_PORT` (optional, default: `8991`): Local HTTP listening port.
- `CLEANER_TRIM_ASCII_PUNCT` (optional, default: `true`): Whether to trim spaces following ASCII punctuation (`U+0021-U+002F`, `U+003A-U+0040`, `U+005B-U+0060`, `U+007B-U+007E`).
- `CLEANER_TRIM_CJK_SPACES` (optional, default: `true`): Whether to collapse spaces between consecutive CJK ideographs (`\u4e00-\u9fff`, `\u3400-\u4dbf`).

## Unicode Punctuation Ranges Used

1. `\u3000-\u303F`: CJK Symbols and Punctuation (e.g. `。`, `、`, `《`, `》`)
2. `\uFF01-\uFF65`: Fullwidth Forms (e.g. `，`, `！`, `？`, `：`, `；`)
3. `\u2000-\u206F`: General Punctuation (e.g. `——`, `……`, quotes)
4. `\uFE10-\uFE1F`: Presentation Forms for Vertical Lines
5. `\uFE30-\uFE4F`: CJK Compatibility Forms
6. `\u0021-\u002F`, `\u003A-\u0040`, `\u005B-\u0060`, `\u007B-\u007E`: ASCII Punctuation

## Example Transformation

### Chinese Sample (CJK Fullwidth Punctuation)
- **Before**: `嗯， 这句话就是我实际语音发出来的文字， 它的空格是这样的， 基本上是在一个标点的后面。`
- **After**: `嗯，这句话就是我实际语音发出来的文字，它的空格是这样的，基本上是在一个标点的后面。`

### English Sample
- **Before**: `Uh, hello, this is English test. Maybe they have this issue to.`
- **After**: `Uh,hello,this is English test.Maybe they have this issue to.`

### CJK Ideograph Spacing
- **Before**: `这 是 一 个 测 试`
- **After**: `这是一个测试`
