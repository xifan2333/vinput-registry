# adapter.cjk-trim

Managed local LLM adapter that removes redundant spaces around CJK punctuation and between CJK ideographs in ASR transcripts.

Some speech recognition models (e.g. sherpa-onnx Zipformer Transducer) insert spurious spaces into Chinese output. This adapter operates as a zero-latency command pipe filter (`stdin -> stdout`) for `vinput-daemon`.

**Scope note**: ASCII / English text is intentionally left untouched — no ASCII punctuation normalization and no interior space collapsing. Only leading/trailing whitespace is stripped (`strip()`).

## Entry

- `entry.py`

## Runtime

- command: `python3`
- architecture: Unix command pipe filter (`RewriteWithCommandAdapter`)
- dependencies: Python 3 standard library only (`dataclasses`, `json`, `os`, `re`, `sys`)

## Environment Variables

### Optional
- `CJK_TRIM_COLLAPSE_CJK_SPACES` (optional, default: `true`): Whether to collapse spaces between consecutive CJK ideographs (`\u4e00-\u9fff`, `\u3400-\u4dbf`, `\uf900-\ufaff`).

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
