# KickClipper refinement — 2026-09-22

## Pipeline decisions retained
- Parakeet defaults to 600 s ASR chunks with 4 s overlap and bounded concurrency (up to 8).
- Generic/MAI ASR remains independently configurable (240 s / 5 s defaults).
- ASR chunks and semantic moment windows remain separate concerns.
- Semantic scouting remains 180 s with 60 s overlap; near-empty windows are skipped.
- Successful ASR chunks remain checkpointed so interrupted long jobs resume rather than restart.
- HLS transport remains TLS-verified and bounded; analysis can use compact media rather than full-resolution video.

## Reliability refinements
- Parakeet transient retry backoff now uses jitter to reduce synchronized retry storms across concurrent workers.
- FFmpeg ASR extraction now reports timeout and stderr details instead of surfacing an opaque subprocess error.
- ASR audio bitrate is configurable with ASR_AUDIO_BITRATE (48k default).

## UI refinement
- Shared marketing/account pages, dashboard tokens, studio tokens and landing page moved to a professional light/white system.
- Portfolio/social accounts are preserved and restyled as clean professional cards rather than removed.

## Verification in this sandbox
- Python compileall: PASS.
- unittest discovery: 69 discovered; 67 passed. The remaining 2 could not import because Flask is not installed in this offline sandbox. Dependency installation could not be completed because this sandbox has no package-network access. These are environment/dependency errors, not observed assertion failures.
- Real provider/VOD benchmark was not run because external provider/network access is unavailable here. Do not treat this file as claiming a measured speedup.

## Live dashboard polling / ETA hotfix
- Fixed VOD polling timer dying after the page/tab became hidden or before the VOD tab was active. The timer now remains scheduled and refreshes immediately when the VOD tab/page becomes active again.
- Active VOD jobs now poll about every 2-6 seconds rather than backing off to 10 seconds, while idle pages still back off.
- Worker-provided `eta_seconds` is now preserved in the API progress payload instead of being parsed and discarded.
- Guardian fallback ETA calibrates after two meaningful overall-progress samples (>=1 second) rather than waiting for three samples/3 seconds.

## 2026-09-22 fast-path refinement
- Added `ASR_FAST_CHUNK_COPY=true`: normalized `.m4a/.aac` analysis audio is now split with AAC stream-copy instead of being decoded and re-encoded to MP3 for every ASR chunk. This removes redundant CPU work on the normal VOD path while preserving chunk caching, overlap, retries, and bounded Parakeet concurrency.
- ASR cache identity now includes chunk format and bitrate, preventing stale cache reuse when chunk transport settings change.
- MP3 fallback remains for arbitrary/non-AAC inputs and is limited to one FFmpeg encoding thread per concurrent chunk to avoid CPU oversubscription.
- Parallel HLS ingest now has bounded whole-download retries, fragment retries, and socket timeout controls. This prevents transient segment failures from immediately killing a long ingest without creating infinite retry loops.
- Progressive scouting was already overlapped with transcription in the existing architecture; it was preserved. The remaining full-audio-ingest barrier was not replaced with speculative multi-range HLS fetching because that can duplicate downloads and is source/CDN dependent. Benchmark transport changes against real Kick VODs before enabling them by default.
