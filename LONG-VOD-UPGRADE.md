# Long VOD pipeline upgrade — 14 September 2026

Integrated into the existing Flask app, account database, routes, clip editor and render pipeline. No application rewrite. No database migration. Existing secrets, accounts and media are not replaced.

## Processing path

Kick VOD page → existing recording resolver → audio-only ingest → reusable mono 16 kHz audio → overlapping 240-second audio cores (5-second padding on each side) → bounded ASR workers → timestamp/text overlap reconciliation → semantic conversation windows → high-recall scout → detailed scoring → targeted uncertain-transcript repair → bounded context/optional visual review → ranking/deduplication → selected video ranges → existing caption/layout renderer.

Local files now get an audio copy without transcoding the full video. Existing other-platform download fallbacks remain available; those fallbacks can still require a full download when no direct media stream is available. This is not a new universal platform resolver.

## Concurrency and memory

Cloud ASR defaults to four workers, reduces request pressure after rate limits/timeouts and gradually recovers after successful requests. The scheduler submits only worker-count audio tasks. Local Whisper uses one model and one audio worker on this PC to avoid multiplying memory/CPU contention. Scout work can run alongside ASR. Local ASR is not falsely advertised as four simultaneous model instances.

Decoded audio is limited to a chunk; the multi-hour decoded video is never loaded into Python. Transcript metadata grows with spoken words, as expected. Chunk MP3s are removed on completion/failure/cooperative cancellation. Normalized audio and completed JSON checkpoints are intentionally retained for resume; selected video sources remain for the editor. MAX_TEMP_GB bounds per-ingest audio and active chunk budgets, not all accumulated account media or a global disk quota. Delete unwanted library clips through the existing controls.

## Context, confidence, scoring

Words retain absolute source timestamps for analysis, provider confidence/language/speaker labels when supplied, and overlap disagreement flags. Source-relative transcripts remain available in the legacy transcript.json format for backward compatibility; .pipeline contains the global version. No fabricated diarization. MAI diarization remains disabled.

Overlaps use normalized text and nearby timestamps, prefer higher-confidence matches and preserve unique words. Conflicting words are flagged for review. This is heuristic alignment, not a proof that all ASR boundary errors disappear. Semantic windows use sentence/pause boundaries and include surrounding context. Completed contiguous chunks feed preliminary scouting while later chunks transcribe; windows wait for forward context. Final scanning fills deferred windows and reuses completed scout calls.

The scout uses a permissive score threshold rather than a strict top-15% cap. That supersedes the earlier diagram percentage to preserve high recall under the detailed specification. Failed scout responses route the affected region to deep analysis. JSON candidates contain timestamps, reasons and requested context extensions. Detailed scoring uses the existing prompts plus labelled context; optional context review refines boundaries with validated evidence and a 180-second bound. The existing configurable context-review limit remains in force. VOD export no longer silently truncates a selected event to 45 seconds.

Primary weights now match the request: judgment 30%, comment/debate 25%, hook 20%, emotion 15%, standalone 10%. Payoff remains separately scored: a payoff below 3/10 caps the result below the normal export threshold. This editorial preference favors controversy over quiet comedy; it is not an empirically validated viral probability. Novelty and technical quality remain available in score breakdowns.

Only low-confidence or overlap-disagreement intervals within promising candidates receive a bounded local wider-beam retry, up to eight intervals of at most 60 seconds by default. A demonstrably lower-confidence or empty retry does not replace the original. Silence-with-speech detection, complete semantic gibberish detection and calibrated cross-provider confidence comparison are not claimed. Acoustic review currently measures RMS and peak amplitude; it does not recognize laughter, shouting or speaker identity. Optional existing frame/Gemini review remains selective.

## Checkpoints and resume

Each ASR chunk has PENDING/PROCESSING/COMPLETED/FAILED state and reusable words. Re-submitting the same source/range reuses completed normalized audio, valid ASR results, scout/detailed model calls and completed exports. Failed/incomplete chunks are retried. The account namespace is retained. No automatic paid job launch after a server restart: submit the same VOD again to resume, or use existing explicit transcript/checkpoint recovery. Completed exports keep their original account/library ownership and are not duplicated into a new job.

Cancellation is cooperative through a per-job marker; the current bounded provider/model operation may need to return before cleanup finishes. The audio-ingest watchdog detects stalls even when FFmpeg stops printing. Abrupt OS termination cannot guarantee immediate finally-block cleanup; incomplete per-chunk files are overwritten on resume. Rendering has a separate configurable worker bound. This remains the existing local app, not a new distributed multi-tenant queue or global compute scheduler.

## Observability

Progress labels include extraction, transcription counts, preliminary scouting, detailed scoring, review, ranking and rendering. Percentages describe individual stages. Final status still comes from actual indexed clips. Per-source .pipeline/timings.json records preparation, ASR plus progressive scout, final scout/deep work, review/visual work, rendering, total time and real-time factor. ASR cache timings separately record chunks, concurrency and merge time. Overlapping stages are intentionally reported as overlapping wall time, not summed as independent durations.

## Files

Changed: config.py, transcribe.py, mai_transcribe.py, vod.py, detect.py, scoring_policy.py, context_review.py, app.py, benchmark.py, templates/index.html, .env.example, tests/test_studio.py, UPGRADE-NOTES.md.

New: audio_pipeline.py, audio_signals.py, pipeline_state.py, scout_pipeline.py, transcript_quality.py, render_checkpoint.py, benchmark_profiles.py, tests/test_long_pipeline.py, this guide.

Removed: none. Database migrations: none. New pip dependencies: none.

## Configuration

All new variables are documented in .env.example: ASR_CHUNK_SECONDS, ASR_CHUNK_OVERLAP_SECONDS, ASR_MIN_CONCURRENCY, ASR_MAX_CONCURRENCY, ASR_RETRY_COUNT, ASR_TIMEOUT_SECONDS, ASR_QUALITY_MIN_CONFIDENCE, ASR_QUALITY_RETRY_LIMIT, SEMANTIC_WINDOW_SECONDS, SEMANTIC_WINDOW_OVERLAP_SECONDS, SCOUT_MAX_CONCURRENCY, SCOUT_MIN_SCORE, SCOUT_MODEL, DEEP_SCORING_MODEL, DEEP_ANALYSIS_MAX_CONCURRENCY, VISUAL_VERIFY_MIN_SCORE, MAX_RENDER_CONCURRENCY, MAX_TEMP_GB.

Blank scout/deep model settings retain the current provider model with different prompts; no unsupported stronger model is silently selected. A separately configured deep model must be supported by the same scoring provider. MAI still needs a valid Azure endpoint/key; this update does not complete Azure enrollment.

## Commands on this computer (CMD)

Wait for the current Clavicular job before restarting the server. Do not launch a second server on port 5000.

```bat
cd /d "C:\Users\james\Documents\Codex\2026-09-09\eve\outputs\kickclipper-demo-unlimited\kick-clipper"
"C:\Users\james\kick-clipper\venv\Scripts\python.exe" -m unittest discover -s tests
```

After stopping the existing server, run:

```bat
"C:\Users\james\kick-clipper\venv\Scripts\python.exe" app.py
```

Open http://127.0.0.1:5000/dashboard. No reinstall is required on this computer. On a new computer extract the clean ZIP, enter its kick-clipper directory, run Setup.cmd once and then Start.cmd. Follow TRANSFER-SETUP.md; the clean ZIP excludes private .env, database and output media. Keep the existing account backup separately when moving its library.

## Tests and benchmark limits

Verified: 67 automated tests passed; 40 application modules parsed/compiled; isolated admin settings, secret non-disclosure and worker handoff passed. Existing database inspected read-only: one account and 98 indexed clips. Current Clavicular processing was left running; server was not restarted.

The suite includes 10-minute/two-hour/simulated six-hour scheduling, global timestamp 7200+13.42, overlap/confidence handling, semantic-boundary context, duplicate candidates, failed chunk retry, 429 handling, interruption/resume, targeted low-confidence repair, cancellation and temporary-file cleanup, bounded decoded-chunk scheduling, progressive scouting and a real FFmpeg captioned export with fixture AI/ASR responses. The latter validates integration/rendering, not funded provider accuracy.

benchmark.py measures actual ASR throughput. benchmark_profiles.py retains the fastest measured profile meeting supplied WER, candidate-recall and boundary-error targets. It needs labelled measurements; no fabricated quality results or automatic unmeasured production tuning. No six-hour real-world speed/WER/recall benchmark has been completed, and eight hours within 30 minutes is not guaranteed.
