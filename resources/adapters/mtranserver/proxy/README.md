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
- `MTRAN_URL` (optional, default: `http://localhost:8989`): Upstream MTranServer endpoint.
- `MTRAN_TOKEN` (optional): Bearer authorization token for upstream MTranServer if authentication is required.

## Notes

- This resource is intended to be materialized into local config and executed locally.
- Configuration guidance for users should be derived from the env list above.
