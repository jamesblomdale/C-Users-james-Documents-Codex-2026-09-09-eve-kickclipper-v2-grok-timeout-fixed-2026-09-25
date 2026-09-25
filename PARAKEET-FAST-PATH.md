# Together Parakeet fast path

This build changes Parakeet from a best-effort add-on into a fail-fast production path.

The previous build could feel slow if Together failed and `TRANSCRIPTION_FALLBACK=whisper` silently moved work onto local Whisper. It also used generic 240-second chunks, creating unnecessary API and FFmpeg overhead.

New behavior:
- Real Together API preflight before a long job starts.
- Admin save validates the exact key/model before saving.
- Clear 401/403/402/413 errors.
- Local Whisper fallback is OFF by default for Parakeet.
- 30-minute Parakeet chunks with 8-second overlap by default.
- Bounded parallel requests and adaptive backoff remain enabled.
- Logs prove the provider and time each request.

Verify before processing:
```bash
python verify_parakeet.py
```
Expected log markers:
```text
[parakeet] VERIFIED model=nvidia/parakeet-tdt-0.6b-v3 ...
[asr] ACTIVE provider=Together/Parakeet ...
[parakeet] OK 00000.mp3 ... in X.XXs
```
If these are absent, the job is not using Together Parakeet.
