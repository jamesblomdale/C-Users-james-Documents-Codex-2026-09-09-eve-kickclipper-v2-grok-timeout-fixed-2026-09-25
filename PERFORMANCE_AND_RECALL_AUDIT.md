# KickClipper R4 performance and recall audit

The supplied project already has a bounded, resumable architecture: source resolution and audio ingest feed `audio_pipeline.py`; Parakeet/MAI/local ASR create timestamped words; per-chunk results are cached; `scout_pipeline.py` performs loose discovery; `detect.py` performs deeper scoring; `moments.py` merges and deduplicates; optional review/vision runs before `vod.py` renders selected ranges. The dashboard receives worker progress through `app.py` and `guardian.py`.

## Refinements applied

- Dashboard polling now uses no-cache requests and continues its timer chain when tabs are hidden or changed. Active jobs poll quickly enough for progress and log updates without a manual refresh.
- ETA is calculated from stage progress when overall progress is still rounded to a low band. The API marks that estimate as `stage`; worker-provided estimates override it. This avoids presenting an audio-stage estimate as a calibrated whole-job forecast.
- Progress events are retained in the job log.
- Scout filtering uses indexed timestamp slices, preserves thin IRL speech signals, and deduplicates identical windows before remote scoring.
- Timing writes use an explicit metrics path rather than mutating the process-wide `JOB_METRICS_PATH`, removing a cross-worker environment race.
- Progressive ASR only rebuilds the contiguous transcript prefix when that prefix advances, and emits a final transcription completion event.
- Audio cache markers use atomic writes and reject path-like cached filenames.

## Defaults and limits

The project keeps its existing bounded ASR/HLS controls and does not increase concurrency without provider evidence. The supplied code includes the R4 IRL recall signals and cached scout path. A 6–12 hour paid-provider benchmark, rolling HLS-to-ASR wall-clock benchmark, and multi-camera evaluation were not run in this environment, so no target speed or accuracy guarantee is claimed. Optional competitor/oracle and visual enrichers remain fail-open.

The supplied audit requested `temporal_memory.py` and `quality_gates.py`; those files are not present in this ZIP, so they were not invented. The actual existing modules were audited and refined instead. ASR/Parakeet now accepts a ceiling of 16; the adaptive gate still halves on 429/5xx/connection pressure and recovers slowly, so 16 is an opt-in ceiling rather than a promise that 16 requests will be faster for every account.

The R4 default was corrected from 12 to 8 HLS fragment workers with a 6-worker fallback and a 1 MB transfer buffer. The live log reports one-percent download buckets. This is a throughput/reliability correction, not a claim that 8 is universally fastest; CDN and network behavior still determine the result. Existing `.env` files must be updated because `.env.example` does not overwrite them.

Validation run on this extracted source:

```text
python -m unittest discover -s tests -q
Python compilation passed. The supplied suite ran 84 tests; 80 passed and 4 existing tests failed before any clean end-to-end benchmark: `test_cap_limit_and_context`, `test_progressive_scout_waits_for_forward_context`, `test_short_punchline_reaches_scorer`, and the older job-wide ETA expectation. Those failures reflect mismatched baseline scoring/progressive behavior in this ZIP and are recorded rather than hidden. The polling and cache refinements were checked separately with focused regression tests.
```

