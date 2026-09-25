# Final speed, reliability, ETA and portfolio update — 2026-09-23

## What changed
- Default VOD ingest is now one full audio-first HLS acquisition (`ROLLING_REMOTE_ASR=false`) rather than many independent remote HLS window pulls. The existing 16-fragment downloader remains the fast acquisition path and ASR still runs bounded/concurrent after audio is available.
- Parakeet keeps 240-second windows with 15-second overlap. This is intentionally separate from the UI's legacy 30-second live/local chunk selector.
- Waitress now serves the Flask app with 8 HTTP threads by default. Heavy VOD/live work remains isolated in child processes.
- Active VOD status polling is 10s, clip polling 25s, and account polling pauses while a VOD is active to reduce HTTP/SQLite churn.
- Scout malformed JSON/provider errors no longer automatically promote an entire window to expensive deep analysis. JSON extraction is more tolerant; a local high-recall signal fallback keeps only plausible windows alive. Empty/weak malformed windows are skipped.
- Dashboard keeps exactly one job-wide ETA. Stage-local audio/fragment ETAs do not replace it.
- Portfolio globe is now a restrained light-blue/white 3D carousel: slow auto-rotation, drag, wheel/arrow controls, depth/opacity, hover pause, and reduced-motion support.
- Added final light-theme overrides so inherited purple/dark-blue accents are converted to the controlled light-blue system.

## Important architecture note
The previous rolling-remote mode optimized time-to-first-transcript but made every ASR window independently read HLS. That can duplicate CDN/HTTP startup and media extraction work. This final default optimizes total-job throughput by acquiring analysis audio once, then using bounded concurrent ASR. Rolling mode remains available as an opt-in experiment.

## Evidence used
- FFmpeg HTTP supports persistent connections (`multiple_requests`) and current libcurl integration can reuse connection caches/multiplex when available.
- NVIDIA documents Parakeet-TDT V3 as a fast offline ASR model with long-form support and timestamps.
- The repository already isolates VOD jobs in subprocesses; therefore the HTTP-server change is for dashboard responsiveness/contention, not a replacement for the media worker architecture.
