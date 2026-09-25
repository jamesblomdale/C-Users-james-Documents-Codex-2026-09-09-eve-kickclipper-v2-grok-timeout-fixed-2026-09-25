# Audio / chunking / total ETA refinement — 2026-09-23

## Audit result

The rolling build already uses the right high-level architecture for long VODs: transport chunks are independent from semantic windows, ASR work is bounded/checkpointed, completed contiguous transcript is sent to ProgressiveScout, and a full-VOD audio download is not required first.

The active Parakeet defaults are 240-second ASR windows with 15 seconds overlap and up to 16 cloud workers. These values were retained deliberately. Four-minute windows keep time-to-first-transcript low while still amortizing API/upload overhead; changing them without a provider/VOD benchmark would be guesswork. Semantic analysis remains independent at 120 seconds / 60 seconds overlap.

## Refinements in this build

1. Rolling remote ASR now tries to resolve an audio-only HLS rendition once with yt-dlp before launching ASR windows. This avoids making the transcription path consume a video-heavy rendition when the master playlist exposes a separate audio track. It fails open to the original stream URL.
2. Remote FFmpeg HLS windows explicitly enable persistent HTTP connections and multiple HLS HTTP connections.
3. Remote FFmpeg windows reconnect on TCP/TLS failures and 429/5xx responses, honor Retry-After, and keep the reconnect delay bounded.
4. The dashboard now exposes one ETA only: total job ETA. Stage-local scan/audio/fragment ETAs are not allowed to overwrite it.
5. Total ETA is based on measured movement of the monotonic job-wide progress bar and waits for enough samples before appearing.

## What was intentionally NOT changed

- The progress percentages are not faked.
- Parakeet chunk duration remains 240 seconds and overlap remains 15 seconds.
- Semantic windows and moment-scoring policy are unchanged.
- Provider concurrency remains separately bounded/adaptive; a higher configured maximum is not treated as proof of higher real throughput.
- The old whole-file HLS downloader remains as a fallback for non-rolling paths.

## Validation

- `python -m compileall -q .` passes.
- The dedicated job-wide ETA regression test passes.
- Full discovery runs 70 tests. The same existing scoring/progressive-scout expectation failures remain, plus two environment-dependent Flask import errors in this audit container. No production scoring values were changed to force stale tests green.
- The installed FFmpeg build used for validation exposes `http_persistent`, `http_multiple`, `reconnect_on_network_error`, `reconnect_on_http_error`, and `respect_retry_after`.

## Important benchmark limitation

No honest wall-clock speed claim is made without the user's real Kick CDN path, Together account limits, and representative multi-hour VOD. This change removes avoidable media/network overhead and improves transport recovery; actual throughput still depends on the source CDN, local connection, FFmpeg behavior for that playlist, and provider rate limits.
