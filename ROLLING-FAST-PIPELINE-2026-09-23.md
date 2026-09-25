# Rolling fast pipeline refinement

This build removes the full-audio-download barrier for direct HLS VODs. With `ROLLING_REMOTE_ASR=true`, the ASR scheduler range-pulls timestamped audio windows from the remote HLS source and sends completed windows to the configured ASR provider concurrently. ProgressiveScout receives contiguous completed transcript windows while later windows are still being fetched/transcribed.

## Defaults
- HLS fallback path: 16 concurrent fragments, fallback 12, hard max 16, 4M yt-dlp buffer.
- ASR: existing configured Parakeet/Together concurrency up to 16 remains supported.
- Rolling remote ASR is enabled by default for full direct-HLS jobs.
- Range-limited jobs retain the existing ingest path.
- Each completed ASR chunk remains checkpointed, so successful chunks can be reused after restart.

## Why this is faster
The previous direct-HLS path completed a whole-VOD parallel audio source pull before `transcribe_vod()` began. The new path removes that serial barrier: multiple small audio windows can be fetched/transcribed immediately. The old parallel HLS downloader remains as a fallback path.

## Progress
`audio_pull` completes immediately in rolling mode because there is intentionally no whole-file audio pull. Real work is reported by the transcription progress and ProgressiveScout. This does not fake download completion; it changes the pipeline so a full audio download is no longer a prerequisite.

## Validation
Python compileall passes. The repository unittest suite runs 70 tests in this audit environment; 65 pass, 3 pre-existing scoring/progressive-scout expectation tests fail, and 2 Flask-dependent tests cannot import because Flask is not installed in the audit environment.
