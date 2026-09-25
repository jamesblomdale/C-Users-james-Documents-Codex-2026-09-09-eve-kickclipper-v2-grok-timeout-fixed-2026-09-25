# Clavicular VOD performance audit — 2026-09-15

## Measured job

- Source: 196.47 minutes
- Started: 06:40:03
- Finished: 08:43:25
- Result: 41 clips produced, 0 clip failures
- Total wall time: 123 minutes

## Measured stage times

| Stage | Wall time | Finding |
|---|---:|---|
| Resolve and audio ingest | 49 min | Kick had no audio-only rendition. FFmpeg fetched a muxed stream serially. |
| Parakeet transcription | 99 sec | 20 ten-minute chunks. This was already fast and was not the bottleneck. |
| Progressive scout + deep window scoring | about 9 min | 246 semantic windows, 189 raw candidates, 8 failed scoring windows. |
| Merge and merged-moment re-score | about 29 min | Merged groups were re-scored with one serial API request per group. |
| Context review and ranking | under 1 min | Ten optional reviews and one ranking call. |
| Fetch and render 41 clips | about 35 min | Every selected 1080p range was downloaded, encoded and titled. |

## Verified source characteristics

The actual Kick manifest contained only muxed audio/video renditions:

- 160p: about 323 MiB at 230 kbit/s
- 360p: about 885 MiB at 630 kbit/s
- 480p: about 2.04 GiB
- 720p60: about 4.78 GiB
- 1080p60: about 12.43 GiB

The transcription ingest does not need high-resolution pixels. The new ingest
selects the 160p rendition and downloads eight HLS fragments concurrently.
An end-to-end fetch of the actual 196-minute Clavicular source completed in
91.27 seconds and produced a valid 340.7 MB H.264/AAC file. A sampled extraction
produced valid 16 kHz mono AAC audio.

## Changes

1. Parallel HLS ingest with bounded concurrency and lowest-bandwidth fallback.
2. Compatible sequential FFmpeg fallback for unusual manifests.
3. Monotonic ingest progress reporting.
4. Merged-moment scoring now uses batches of eight with four concurrent calls.
5. Customer jobs are capped at 12 rendered clips by default. The owner can
   choose another value, including unlimited. Discovery and transcript reuse
   remain complete; the cap controls expensive final exports.

## Expected impact

For this exact source, media ingest fell from 49 minutes to 91 seconds in the
live benchmark. The 29-minute serial merge pass is expected to become a small
number of concurrent batch rounds. Parakeet remains unchanged because it took
only 99 seconds. A 12-clip customer export avoids the previous 35-minute cost
of producing 41 full-HD clips.

Exact production completion time still depends on CDN throughput, model API
latency and worker CPU/GPU. A universal ten-minute guarantee is not supportable
on arbitrary customer hardware, especially when dozens of clips are requested.
