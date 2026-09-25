# KickClipper fast VOD pipeline — 2026-09-22

This build keeps the premium white/blue UI and IRL rating refinements, while changing the default ASR profile for faster first text and cheaper retries.

## Active fast path

1. Resolve Kick HLS and prefer audio-only / lowest-bandwidth audio-capable rendition.
2. HLS fragment fetch stays capped at 8 for this deployment because the previous 16-worker setting was unstable on the user's machine.
3. Produce compact mono AAC at 32 kbps; never create a full PCM/WAV VOD for the normal path.
4. Parakeet VOD chunks are 240 seconds with 15 seconds overlap, max 8 Together calls in flight.
5. Each completed contiguous chunk is merged immediately and fed to ProgressiveScout; scouting does not need to wait for the entire transcript.
6. Semantic talk windows default to 120 seconds / 60 second hop. IRL event scoring/merging remains tighter through the IRL rating profile.
7. Deep scoring runs only on scout survivors; merge/re-score follows; vision is selective; rendering happens only for winners.
8. ASR chunk JSON is checkpointed. Restarting the same source resumes completed chunks instead of retranscribing them.

## Network-error handling

Together HTTP sessions use keep-alive/connection pooling. A ConnectionError/Timeout closes the stale worker session and now retries on a short reconnect schedule (0.25s, 0.75s, 1.5s, 3s, 6s, 10s). HTTP 429/5xx responses continue to use provider-aware backoff and AdaptiveGate pressure reduction. This avoids treating a dropped socket like a long provider rate-limit event.

## Why 240s / 15s

The old 600s / 4s profile reduced request count but made time-to-first-text and failed-request recovery worse. Four-minute jobs keep all 8 provider slots useful, make retries much cheaper, and the larger overlap protects sentence seams. The model itself can handle longer audio; these chunks are for pipeline latency/reliability, not model context.

## Intentional deviations from the research note

- Together concurrency is NOT raised to 12–24 because this account is explicitly limited to 8.
- HLS concurrency is NOT raised to 32–64 because 16 already caused instability in this deployment. It remains 8.
- Full VAD-aligned rolling HLS windows are not claimed as implemented here. The current stable ingest still materializes compact audio before the chunk scheduler. A true segment-to-ASR streaming ingest is a larger architectural change and should be benchmarked separately rather than mixed into a reliability patch.
- Word timestamps stay enabled because KickClipper needs caption-accurate clip boundaries.

## Next benchmark target

The next architectural speed upgrade is a rolling HLS segment scheduler that releases each 3–4 minute compressed audio window directly to Parakeet while later HLS segments are still downloading. It should preserve the same 8-call cap, checkpoints, and progressive scout. Benchmark it against this stable build before replacing the current ingest.
