# KickClipper Long-VOD Speed Architecture Update

## Goal
Target a 2-hour VOD analysis path in roughly 5–10 minutes on healthy network/API capacity, and a 3-hour VOD in roughly 8–15 minutes. This is a target, not a guaranteed SLA: HLS download speed, Together serverless rate limits, LLM latency, number of shortlisted moments, and render settings can dominate wall time.

## Why the old build could take ~70 minutes
1. Remote HLS audio was expanded to 16 kHz mono PCM WAV before ASR. Three hours is roughly 346 MB of PCM, creating avoidable network-to-disk and disk I/O.
2. That large normalized file was then re-read and re-encoded into 30-minute MP3 chunks before each Parakeet request.
3. Parakeet used only four concurrent requests. Long 30-minute requests meant a slow request could hold an entire wave of work.
4. The transcript scout made one LLM request per semantic window. With 180-second windows / 60-second overlap, a 3-hour VOD is around 90 scout windows. This is a major round-trip tax even after ASR is fast.
5. Deep scoring used batches of only four and only two concurrent batches.
6. Context/visual review and rendering were conservative and mostly low-concurrency.

## New launch profile
- Compact 16 kHz mono AAC ingest at 48 kbps instead of PCM WAV.
- Together Parakeet remains the primary ASR: nvidia/parakeet-tdt-0.6b-v3.
- Parakeet chunks: 10 minutes with 4 seconds overlap.
- Up to 8 concurrent Parakeet requests, with the existing adaptive gate automatically backing off on 429/5xx pressure.
- Scout windows remain semantic/time-aware, but six independent windows are evaluated per LLM request.
- Up to six scout batches concurrently.
- Deep scoring batches increase from four to eight windows, with up to four concurrent batches.
- Context review remains shortlist-only; default review limit becomes eight.
- Render concurrency increases to two.
- Existing resumable ASR cache, retry/backoff, candidate checkpoints, transcript repair, visual verification, scoring, and clip-quality gates remain.

## Why this is not conventional RAG
A VOD is a bounded chronological event stream, not an unbounded document knowledge base. Vector search/RAG can silently miss the exact payoff or relationship between setup and reaction. KickClipper therefore uses chronological semantic windows + overlap + hierarchical filtering: cheap/high-recall scout -> deeper scoring -> merge/repair -> optional visual/context review -> final ranking. This keeps whole-story context without building a vector database for every VOD.

## Chunking choice
Fixed 10-minute ASR chunks are for transport/inference throughput only. They do NOT define clip boundaries. Clip discovery uses 180-second semantic windows snapped toward sentence/pause boundaries, with overlap, and later merges related candidates. This separates the fastest ASR chunk size from the best editorial context size.

## Reliability preserved
- Per-ASR-chunk disk checkpoints and resume.
- Atomic JSON state writes.
- Adaptive concurrency backoff.
- Retryable HTTP handling.
- Provider preflight before a long job.
- No automatic local Whisper fallback unless explicitly enabled.
- Candidate and render checkpoints.
- Cached LLM analysis.
- Transcript overlap merge and targeted repair.

## Benchmark requirement before launch
Run the same known 2–3 hour VOD three times and inspect `.pipeline/.../timings.json` plus `.asr_cache/.../timings.json`. Record media preparation, ASR, scout/deep analysis, context/visual review, and rendering separately. Do not call the 5–10 minute target achieved until the 95th-percentile run is close to it on production infrastructure.
