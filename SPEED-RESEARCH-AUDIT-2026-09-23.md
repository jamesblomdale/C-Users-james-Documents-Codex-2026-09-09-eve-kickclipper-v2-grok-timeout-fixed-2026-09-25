# KickClipper long-VOD speed/reliability audit — 2026-09-23

## Production defaults in this build
- HLS ingest: 12 concurrent fragments, hard max 16, automatic retry at 8 before sequential fallback.
- Together/Parakeet: 8 concurrent requests. This is deliberately separate from HLS fragment concurrency.
- ASR chunks: 240 seconds with 15 seconds overlap.
- Audio: prefer native AAC and stream-copy/remux; compact 32 kbps AAC fallback only when required.
- Progressive scout starts from contiguous completed ASR chunks; it does not need to wait for the final transcript.
- Scout threshold: 32 for high recall; deep scoring remains strict.

## Fixes made in this audit
1. Removed stale ASR_MAX_CONCURRENCY fallback to MAI_CONCURRENCY=4. Default is now 8.
2. HLS fragment concurrency is now a fast/stable two-step policy: 12 -> 8 -> sequential compatibility fallback.
3. If the fetched HLS source already contains AAC and the whole VOD range is requested, the pipeline remuxes with `-c:a copy` instead of re-encoding hours of audio before ASR.
4. HLS progress logging is bucketed to 5% to reduce log/UI event churn. Downloading itself remains continuous.
5. `.env.example` duplicate performance settings were removed and the high-recall scout threshold corrected to 32.

## Why ASR remains at 8
The existing product/admin validation and the user's verified provider setup are capped at 8. Raising API calls to 12/16 without verified provider headroom increases 429s, connection churn, retries, and tail latency. HLS fragment fetching can safely be tuned independently because it is CDN I/O, not Together inference concurrency.

## Retrieval/chunking strategy
For this clip-discovery workload, full vector-database RAG is not required for each VOD. The existing architecture already behaves like a lightweight hierarchical retrieval pipeline:
- child/search layer: overlapping timestamped semantic transcript windows;
- parent/context layer: before/candidate/after context expansion;
- high-recall scout selects a small fraction;
- deep score only survivors;
- merge related story spans and re-score;
- selective visual verification only for high-value candidates.

For future transcript search across many VODs, store timestamped child chunks and parent event/story spans, retrieve children, then expand to the parent span. Keep source/channel/time/event-class metadata and rerank before deep analysis.

## Remaining architectural ceiling
The ingest stage still completes the selected HLS rendition before cloud ASR begins. The next major speed project is a true producer/consumer segment pipeline: completed HLS segment groups -> audio window -> ASR queue -> progressive scout while later HLS fragments are still downloading. This should be implemented with durable segment checkpoints and backpressure rather than by launching many independent ffmpeg reads against the same remote playlist.
