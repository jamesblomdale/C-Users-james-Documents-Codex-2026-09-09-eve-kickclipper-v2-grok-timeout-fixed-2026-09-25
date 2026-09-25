# KickClipper refinement audit — 16 September 2026

This document records what is implemented and measured. It does not claim a
provider is better without a KickClipper-labelled comparison.

## Current architecture discovered

```text
Flask route + account/billing hold
  -> validate user-owned upload or supported URL
  -> Kick/Twitch/YouTube resolver
  -> lowest-bandwidth parallel HLS ingest or compatible download fallback
  -> compact 16 kHz mono audio cache
  -> overlapping bounded Parakeet/MAI/Whisper ASR chunks
  -> absolute word timestamps + overlap reconciliation
  -> chronological semantic windows independent from ASR chunks
  -> progressive, high-recall transcript scout
  -> batched/concurrent deep scoring
  -> nearby candidate merge + batched merge re-score
  -> targeted uncertain-transcript repair
  -> shortlist-only context/audio/optional visual review
  -> final ranking, deduplication and checkpoint
  -> fetch only selected video ranges
  -> captions + optional tracking/reframing + title generation
  -> clip/database indexing + billing settlement
```

Production components remain in their existing modules. No queue, database,
authentication, billing, source route, rendering or editing path was replaced.

## Baseline verification

- 78 existing tests passed before this refinement.
- The suite covers billing, provider-role separation, ASR cache/resume, chunk
  retries, timestamps, semantic boundaries, candidate validation, cancellation,
  real FFmpeg export, progress, security validation and reversible deletion.
- The historical timing files contain five complete long-VOD jobs.

## Measured bottlenecks from existing jobs

| Job | Source | Media preparation | ASR/progressive work | Deep scan | Review | Render | Total | Clips |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| N3on older | 5.20 h | 57.5 m | 35.9 m | 11.8 m | 0.8 m | 55.5 m | 184.5 m | 42 |
| Sneako older | 7.36 h | 84.3 m | 4.0 m | 38.8 m | 0.9 m | 82.5 m | 284.8 m | 95 |
| Clavicular | 3.27 h | 48.9 m | 1.8 m | 8.3 m | 0.5 m | 34.9 m | 123.2 m | 41 |
| N3on optimized | 9.78 h | 7.2 m | 4.6 m | 8.4 m | 0.6 m | 41.7 m | 65.3 m | 45 |
| Sneako optimized | 7.05 h | 5.6 m | 5.4 m | 4.8 m | 0.5 m | 15.2 m | 34.2 m | 89 |

These are actual application timing files. They prove that transcription is not
the dominant cost on the optimized long runs. Export count and CPU video
encoding dominate, followed by ingest and model analysis. Earlier reports also
identified an unmeasured serial merge re-score; the current code batches it,
and this refinement now records it separately.

## Measurement added

Future source-scoped `timings.json` files now retain:

- provider preflight, media preparation, transcription, transcript merge,
  transcript repair, scout/deep reasoning, merged-moment re-score, context or
  visual review, final ranking and render seconds;
- source/audio duration, transcript words, ASR chunks/concurrency, initial and
  final candidates, clips produced;
- LLM/ASR request counts and accumulated request time, retries, failed requests,
  cache hits, input/output tokens and estimated API cost where a known rate is
  configured;
- real-time factor and speed multiple.

No prompts, transcript text, URLs, media paths, account details or API keys are
written by the metrics layer.

## Golden VOD evaluation

`evaluate_moments.py` compares ranked predictions with human labels and reports:

- candidate recall at 10 and 20;
- precision at 10 and 20;
- useful clip rate;
- mean start and end boundary error;
- temporal overlap used for matching.

Label format is documented under `eval/gold/README.md`. Provider comparisons
must use the same transcript, same gold labels and same render-independent
candidate limit. Human labels are still required; manufacturing labels from a
model would invalidate the evaluation.

## Current technology research

Research date: **16 September 2026**. The following are candidates for a
controlled benchmark, not approved replacements.

### ASR

- **Current Together Parakeet TDT 0.6B** remains the production baseline. On
  measured long jobs it transcribed hours of compact audio in roughly 2–5
  minutes, so replacing it for speed alone is not justified.
- NVIDIA documents Parakeet TDT as offline with word timestamps; current Speech
  NIM also exposes Parakeet CTC/RNNT variants. Self-hosted NIM requires a suitable
  NVIDIA GPU/container deployment. [NVIDIA Speech NIM](https://docs.nvidia.com/nim/speech/latest/asr/deploy-asr-models/index.html)
- **MAI-Transcribe-2** provides word timestamps, diarization, keyword biasing,
  noise robustness and verbatim style, but is a public preview without an SLA.
  It stays an optional provider. [Microsoft MAI-Transcribe-2](https://learn.microsoft.com/en-us/azure/ai-services/speech-service/mai-transcribe)
- **Deepgram Nova-3** supports batch/streaming, word times, keyterms and batch
  diarization. Published comparative WER claims are provider-authored and must
  not decide adoption. [Nova-3 models](https://developers.deepgram.com/docs/models-languages-overview), [diarization](https://developers.deepgram.com/docs/diarization)
- **ElevenLabs Scribe v2** supports word timestamps, up to 32 speakers, audio
  tags and keyterms; the documented maximum is 10 hours/3 GB in standard mode.
  [Scribe v2](https://elevenlabs.io/docs/overview/capabilities/speech-to-text)
- **Groq Whisper large-v3/turbo** documents 189×/216× speed factors and
  $0.111/$0.04 per audio hour, with word timestamps and file limits. These must
  be verified on streamer slang and names. [Groq speech-to-text](https://console.groq.com/docs/speech-to-text)

### Moment reasoning and multimodal review

- The current OpenAI path uses GPT-4o mini for all transcript scoring. It is
  inexpensive and fast, but quality must be measured against labels.
- OpenAI GPT-5.6 Luna is a current low-cost structured-output candidate at
  $0.20/M input and $1.20/M output tokens. It should be tested as a deep judge,
  not automatically applied to every scout window. [GPT-5.6 Luna](https://developers.openai.com/api/docs/models/gpt-5.6-luna)
- Grok 4.3 supports structured output and configurable reasoning, but costs
  $1.25/M input and $2.50/M output tokens. Its default low reasoning can add
  latency to a classification workload unless explicitly configured.
  [Grok 4.3](https://docs.x.ai/developers/models/grok-4.3)
- Gemini 3.8 Flash can inspect video with static or agentic processing. Static
  mode samples frames at 1 FPS and can miss fast action, supporting the existing
  policy of candidate-only visual review. [Gemini video understanding](https://ai.google.dev/gemini-api/docs/video-understanding)

## Architecture decision at this checkpoint

Keep the current audio-first, temporal hierarchical pipeline. It already
separates ASR transport chunks from semantic windows, preserves chronological
context, runs expensive work only after scouting, caches source-scoped results
and renders selected ranges. Traditional vector RAG is not introduced because
it offers no measured benefit and may separate setup from payoff.

The next model or chunking change must pass the same labelled VOD evaluation.
Recommended experiment order:

1. Compare current OpenAI and Grok scoring on cached transcripts with zero ASR
   or rendering cost.
2. Compare Parakeet against one diarization-capable ASR on the same noisy,
   manually corrected 15–30 minute samples.
3. Add speaker/event scene construction behind a feature flag only after the
   labels expose failures caused by the current semantic windows.
4. Add targeted Gemini review only for high-scoring candidates whose transcript
   does not explain the event.
5. Benchmark hardware video encoding, because measured rendering is currently
   a larger launch risk than ASR.

## Known limitations

- No human-authored golden labels were present, so quality deltas cannot yet be
  reported honestly.
- No paid multi-provider benchmark was run; that requires provider credentials
  and incurs external charges.
- CPU/GPU utilisation and peak memory are only partially available on this
  Windows worker and are not yet sampled continuously.
- Historical jobs predate the new request/token telemetry, so their API cost
  cannot be reconstructed exactly.
- Strict 10-minute completion cannot be guaranteed when the user requests many
  CPU-encoded, tracked clips. Time-to-ranked-moments and time-to-all-renders must
  remain separate launch metrics.

## Rollback

The production algorithm was not replaced. To remove this refinement, revert
`pipeline_metrics.py`, the metrics calls in `pipeline_state.py`, `detect.py`,
`parakeet_transcribe.py`, `audio_pipeline.py`, `scout_pipeline.py` and `vod.py`.
`evaluate_moments.py`, `eval/` and its tests are offline-only and may simply be
deleted.

