# Parakeet transcription provider

This build adds Together AI NVIDIA Parakeet TDT 0.6B v3 as a first-class VOD ASR provider.

## Enable

Set in `.env`:

```env
TRANSCRIPTION_PROVIDER=parakeet
TRANSCRIPTION_FALLBACK=whisper
TOGETHER_API_KEY=YOUR_KEY
PARAKEET_MODEL=nvidia/parakeet-tdt-0.6b-v3
PARAKEET_LANGUAGE=en
PARAKEET_CONCURRENCY=4
ASR_MAX_CONCURRENCY=4
```

Parakeet uses the existing long-VOD scheduler: overlapping audio chunks, bounded parallel cloud calls, durable per-chunk checkpoints, global timestamps, overlap de-duplication, progressive transcript delivery, retries, and optional local Whisper fallback.

The provider requests `verbose_json` with word timestamps. Speaker diarization is optional (`PARAKEET_DIARIZE=true`) but is off by default until validated on the production account/model.

Do not put a real API key in source control.
