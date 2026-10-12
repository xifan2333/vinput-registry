# adapters.mtranserver.proxy

Managed local LLM adaptor that exposes MTranServer through an OpenAI-compatible API.

## Entry

- `entry.py`

## Runtime

- command: `python3`
- provides local endpoints compatible with:
  - `GET /v1/models`
  - `POST /v1/chat/completions`
- dependencies: Python 3 standard library only (`http.server`, `urllib.request`, `json`, `uuid`, `time`)

## Environment Variables

### Required
- `MTRAN_PORT` (required): Local HTTP listening port (e.g. `8990`). The script requires an explicit port and will exit with error if omitted.

### Optional
- `MTRAN_URL` (optional, default: `http://127.0.0.1:8989`): Upstream MTranServer endpoint.
- `MTRAN_TOKEN` (optional): Bearer authorization token for upstream MTranServer if authentication is required.

## Scene Prompt Specification

The scene prompt should include the directive `translate to <target_lang>`:

- Examples:
  - `translate to en`
  - `translate to zh`
  - `translate to ja`
  - `translate to de`

The target language code is parsed via case-insensitive regex (`translate to ([\w-]+)`). If the prompt does not match this pattern, the target language defaults to `en`.

The adapter extracts text enclosed in `<vinput-asr>...</vinput-asr>` (standard vinput ASR packaging) and strips trailing constraints instructions, forwarding only the clean transcribed text to the upstream MTranServer `/translate` endpoint.

## Notes

- This resource is intended to be materialized into local config and executed locally.
- Configuration guidance for users should be derived from the env list above.
