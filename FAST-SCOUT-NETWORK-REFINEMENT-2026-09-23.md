# Fast scout + ASR network refinement

Scout now batches 6 semantic windows per provider request rather than one request per window. A 288-window cold scout therefore targets about 48 provider calls before retries rather than about 288. Per-window timestamp validation, caching, progress and selective fallback remain.

Parakeet can retain a configured ceiling of 16, but now starts at 8 concurrent requests and ramps upward after stable successes. Existing adaptive pressure handling backs concurrency down after network/retryable pressure.

Defaults:
SCOUT_WINDOWS_PER_BATCH=6
SCOUT_MAX_CONCURRENCY=6
ASR_INITIAL_CONCURRENCY=8
ASR_MAX_CONCURRENCY=16
PARAKEET_CONCURRENCY=16

## 2026-09-23 span/provider-pressure patch
- Added `scout_span.py` to repair relative seconds, swapped start/end, millisecond timestamps, and slight overrun safely.
- Provider candidates are hard-clamped to the known transcript/VOD end.
- Invalid candidates are dropped unless a local signal gate justifies a short <=45s fallback; the raw ~120s semantic window is never promoted as fallback.
- Scout cache identity now uses model + exact window start/end so later forward-context growth does not re-call the LLM for the same window coordinates.
- Progressive scout waits for enough accumulated transcript before launching, avoiding tiny scout batches after every ASR update.
- Provider roles remain separated: Together/Parakeet is transcription only; Grok/OpenAI is scout/ranking only.
- Conservative defaults: Parakeet 8, scout concurrency 2, scout batch 2, deep concurrency 2, context review 4, rolling remote ASR off.
