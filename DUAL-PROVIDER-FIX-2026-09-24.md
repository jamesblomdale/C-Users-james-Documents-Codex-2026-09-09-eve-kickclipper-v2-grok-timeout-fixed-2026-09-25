# Dual-provider routing fix — 2026-09-24

Canonical roles:
- Transcription: Parakeet via Together (`TOGETHER_API_KEY`)
- Scout + primary/deep scoring: OpenAI `gpt-5.4-mini` via `OPENAI_API_KEY`
- Judge/title: xAI `grok-4.6` via `XAI_API_KEY` / `GROK_API_KEY`

Important behavior:
- Provider routing is derived from the model name, not the legacy global `LLM_PROVIDER`.
- `gpt-5.4-mini` can no longer be sent to the xAI endpoint.
- `grok-4.6` can no longer be sent to the OpenAI endpoint.
- Saving a Grok key in Admin does not switch the primary GPT scout/scorer away from OpenAI.
- OpenAI Admin validation checks model access with `GET /v1/models/gpt-5.4-mini`.
- OpenAI inference uses the Responses API; xAI Grok uses its chat-completions endpoint.
