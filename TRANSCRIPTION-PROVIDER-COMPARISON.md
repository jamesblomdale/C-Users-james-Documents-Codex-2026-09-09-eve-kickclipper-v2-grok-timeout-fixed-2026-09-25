# Transcription provider decision (20 September 2026)

## Decision

Keep `TRANSCRIPTION_PROVIDER=parakeet` as the production default. Do not use
Wispr Flow or Grok Speech-to-Text as the default long-VOD engine yet.

## Comparison

| Option | Fit for multi-hour Kick VODs | Published price | Evidence and trade-off |
|---|---|---:|---|
| Together AI Parakeet TDT 0.6B v3 | Direct fit; already integrated with retries, word timestamps, chunk checkpoints, and bounded concurrency | $0.0015/minute (about $0.09/hour) | Together describes it as high-throughput and provides word-level timestamps. Independent benchmarks report very fast latency, but noisy multi-speaker WER varies by dataset. |
| xAI Grok Speech-to-Text | Potential future provider; not integrated in this repository | $0.10/hour batch, $0.20/hour streaming | xAI documents batch/streaming STT and speaker diarization. There is not yet a directly comparable public long-form Kick/VOD benchmark in this project, so switching would add integration and regression risk. |
| Wispr Flow | Poor fit; consumer dictation/notetaker rather than a server-side VOD API | Free tier; Pro is $15/month or $12/month billed annually | Flow is designed for dictation in desktop/mobile apps. Its documented session limits and lack of a VOD API make it unsuitable for unattended multi-hour processing. |

## Why Parakeet remains the budget choice

Parakeet is slightly cheaper than Grok batch transcription, already has the
pipeline contracts this app depends on, and avoids paying a separate monthly
dictation subscription. The current architecture can retry failed chunks and
reuse successful transcript checkpoints, which matters more to total job time
than a small per-hour price difference.

## When to revisit Grok

Add it only as an opt-in provider after recording the same benchmark set through
both providers: a clean segment, overlapping speech, noisy stream audio, names
and slang, and a speaker-change segment. Compare WER, word-timestamp coverage,
retry rate, wall-clock RTF, and total cost. A provider should replace Parakeet
only if it wins on the user's actual VOD sample without increasing failure or
latency rates.

Sources checked: Together's Parakeet model page, xAI's Speech-to-Text pricing
announcement, and Wispr Flow's pricing and plan documentation (20 September
2026).
