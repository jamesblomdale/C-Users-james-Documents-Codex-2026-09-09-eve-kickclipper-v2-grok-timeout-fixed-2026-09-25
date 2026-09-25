# N3on performance audit — 2026-09-16

## Actual result

- Source duration: 586.96 minutes (9h 47m)
- Job wall time: 65m 22s
- Output: 45 clips, no failed exports

## Timeline reconstructed from `job_events`

| Time | Stage | Duration / result |
|---|---|---|
| 12:00:10–12:05:05 | Parallel HLS fetch | 4m 55s; 1009.8 MB 160p muxed source |
| 12:05:05–12:07:13 | Compact audio extraction | 2m 8s |
| 12:07:13–12:11:27 | Parakeet ASR | 4m 14s; 59 ten-minute chunks |
| Through 12:15:55 | Progressive/final scout | 604 scout candidates; 591 survived |
| 12:15:55–12:20:12 | Deep scoring | 591 windows in 74 batches; 366 passed |
| 12:20:55–12:22:59 | Batched merge scoring | 254 groups in 32 batches; 97 passed |
| 12:22:59–12:23:40 | Review and ranking | 90 deduped, 45 selected |
| 12:23:40–13:05:21 | Range fetch and render | 41m 41s; 45 1080p clips |

## Problems found

1. The export count was the dominant remaining cost. The owner requested
   unlimited clips, and peak locking caused 45 exports. Each range used the
   highest 1080p60 Kick rendition, then CPU encoding.
2. The scout threshold of 32 rejected none of the 604 proposed candidates.
   This made the scout an expensive pass-through and sent 591 overlapping
   windows into deep scoring.
3. Three simultaneous Parakeet connection errors were treated as three
   independent capacity signals, collapsing concurrency from four to one.
4. ConnectionAbortedError messages during range cutting were expected client
   disconnects after FFmpeg had received the requested interval. They were
   incorrectly logged as transport errors.
5. One Grok title request returned HTTP 429 capacity exhaustion. The clip was
   preserved through the existing title fallback.
6. Several scout/review responses failed JSON or evidence validation. Those
   windows were preserved through conservative fallbacks, so the job remained
   complete, but they added unnecessary deep work.

## Applied changes

- Customer exports default to 12 clips; owner remains able to choose unlimited.
- HLS range sources are limited to 720p60 for final rendering. Audio ingest
  still automatically selects the smallest 160p rendition.
- Scout minimum raised to 60 based on this job's measured score distribution.
  This would have reduced its deep shortlist from 604 to 452 while retaining
  the high-recall range.
- Adaptive ASR pressure now counts simultaneous failures as one incident using
  a five-second cooldown.
- Expected range-request disconnects no longer appear as transport errors.
- Batched/concurrent merged-moment scoring from the prior fix remains active.

## Practical target

For a normal 3-hour customer VOD with up to 12 exports, the measured pipeline
supports a realistic target around 10–20 minutes on this local worker, subject
to Kick CDN, Grok/Together capacity and clip duration. A hard ten-minute SLA
cannot be guaranteed on one desktop CPU. Meeting a strict SLA requires queued
cloud workers, hardware video encoding and enough workers to absorb provider
or CDN variance.
