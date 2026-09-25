# Research and pipeline status

The supplied R4 code has a fast audio-only HLS path and bounded Parakeet work, but the current VOD entry point still waits for `_parallel_hls_fetch()` / `_pull_audio_only()` to finish before calling `audio_pipeline.transcribe_vod()`. That means the present implementation is audio-first, but not yet a true rolling producer/consumer pipeline. A true rolling pipeline needs a source-specific segment queue, a demux-safe rolling audio container, durable segment checkpoints, and a finalization protocol; adding a thread around the current whole-file downloader would not provide that guarantee.

The safe changes in this revision keep the known stable Parakeet concurrency at 8, reduce the default HLS attempt to 8 with a 6-worker fallback, use a larger transfer buffer, report real one-percent progress, preserve chunk checkpoints, and keep scouting indexed/deduplicated. Higher concurrency must be benchmarked against the account and CDN before enabling it.

Research references:

- FFmpeg HLS segment retry design: https://ffmpeg.org/pipermail/ffmpeg-devel/2022-October/303094.html
- FFmpeg persistent HTTP connections for HLS: https://ffmpeg.org/pipermail/ffmpeg-devel/2017-December/222945.html
- FFmpeg protocol options and reconnect behavior: https://www.ffmpeg.org/ffmpeg-protocols.html
- Together Parakeet model reference: https://www.together.ai/models/parakeet-tdt-0.6b-v3

No reliable 2–12 hour benchmark was available in this environment. The package must not promise a specific processing time until a representative Kick VOD is timed end to end with the actual account, CDN route, and machine.
