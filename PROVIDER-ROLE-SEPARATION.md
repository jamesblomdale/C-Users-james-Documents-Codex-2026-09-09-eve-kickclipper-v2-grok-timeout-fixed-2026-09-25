# Provider role separation

## Hard rule
- **Together / NVIDIA Parakeet**: speech-to-text only. It reads `TOGETHER_API_KEY`.
- **Grok (xAI)**: transcript scouting, scoring, titles, and configured visual review only. It reads `LLM_API_KEY`.
- Neither path may borrow the other provider's key.

## Launch configuration
```env
TRANSCRIPTION_PROVIDER=parakeet
TRANSCRIPTION_FALLBACK=none
TOGETHER_API_KEY=...
LLM_PROVIDER=grok
LLM_API_KEY=...
```

The app now validates these roles, subprocesses forward them independently, and role-separation tests assert the correct Authorization key/endpoint.
