# VOD speed and quality refinement

The existing application already uses the intended broad architecture: audio-first ingest, overlapping ASR, transcript word timestamps, semantic windows, a scout pass, deep scoring, candidate merging, selective video rendering, checkpoint files, and reusable caches. The changes in this update are deliberately incremental so working provider, resume, and rendering behavior is preserved.

## Changes in this update

- Added a conservative local scout prefilter (`MIN_SCOUT_WORDS`, default `4`). Windows with almost no recognized speech are skipped before an LLM request. Normal spoken windows still go through the high-recall scout.
- Tightened the default merge gap from `25` to `12` seconds and the merge span from `180` to `120` seconds. This reduces padded multi-minute clips while retaining the existing topic-aware bonus and peak fallback. Both are configurable with `MERGE_GAP_SECONDS` and `MAX_MERGE_SPAN_SECONDS`.
- Kept bounded parallel scoring and batch rescoring; no sequential replacement was made.

## Why this helps

The expensive part of a long VOD is remote transcription/scoring, not Python window construction. Avoiding requests for silence/near-empty transcript windows is safe and cheap. The tighter merge limits reduce downstream render time and prevent a good peak from carrying a long dead lead-in. The existing caches and checkpoints remain the source of truth for resume and re-analysis.

## Limits

No honest benchmark can be claimed without running a representative VOD with the configured provider and machine/network. Full VAD, provider-side batch ASR, diarization, and visual analysis remain optional/provider-dependent; they should be benchmarked before enabling higher concurrency. Increase `ASR_MAX_CONCURRENCY`, `PARAKEET_CONCURRENCY`, or scout concurrency only after checking provider limits and memory.
