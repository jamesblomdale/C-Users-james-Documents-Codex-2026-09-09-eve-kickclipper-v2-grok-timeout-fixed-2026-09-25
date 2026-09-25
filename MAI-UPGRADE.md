# MAI transcription upgrade — September 13, 2026

This implements the final transcription-only specification in your proposal.
MAI is available as a configured provider; local Whisper remains selected until
you supply Azure credentials. Your account database, clips, rating policy,
captions, deletion controls, verified HTTPS transport and Kick link resolver remain.
The current running Blame job must finish before restarting the website.

## Analysis and refinements

Microsoft lists MAI-Transcribe-2 at a limited-time $0.10 per audio hour, and reports
model-inference latency of ten seconds for one hour. Those are not measurements of
this app. Six hours with 24 cores and eight-second padding on both sides uploads
about 6.102 hours: approximately $0.61 at that listed rate, excluding retries,
taxes, download/upload time, LLM calls, storage and rendering. Do not sell a
guaranteed turnaround or permanent price based on that figure.
[Microsoft model information](https://microsoft.ai/models/mai-transcribe-2/)

MAI is preview without a production SLA. Word timestamps and verbatim text are
supported. Microsoft's long-recording diarization warning supports leaving it
disabled here. Phrase-list names are hints, not guaranteed correct spellings.
[Microsoft MAI documentation](https://learn.microsoft.com/en-us/azure/ai-services/speech-service/mai-transcribe)

The synchronous API documents per-file limits of under two hours and 250 MB.
The manager uses 15-minute cores, eight seconds of surrounding audio, and
64-kbps mono MP3. Even the configurable 60-minute maximum stays below the duration
limit with padding; file size is checked before upload.
[REST contract](https://learn.microsoft.com/en-us/rest/api/speechtotext/transcriptions/transcribe?view=rest-speechtotext-2025-10-15)

The app already scores eight windows per request with six concurrent batches.
Replacing that with another purported parallel implementation would not itself
speed it up. Existing shortlisted context/video review is preserved. Deepgram,
speaker identity stitching, semantic scene replacement and multiple independent
judges are not part of this transcription-only change.

Additional safeguards implemented:

- Only each core's midpoint-owned words enter the final chronological transcript.
  This avoids intentionally copying padding words twice. Different recognition
  around boundaries can still affect accuracy and needs real-stream evaluation.
- Invalid or missing timestamps are errors, not fabricated zero-time words or
  silently treated as no speech. A well-formed silent result is accepted.
- Successful normalized results, including local fallback, are saved atomically.
  Cache identity includes source path/size/mtime, model/request settings, chunk
  settings and fallback profile. Malformed cache files are recomputed.
- Completed audio ingestion is reused for the same Kick/HLS reference and range,
  so retrying an interrupted MAI VOD can actually reach its existing chunk cache.
  Local-file reuse requires the same unchanged file. Changed URLs/ranges/settings
  produce separate caches; this is ASR resume, not scoring/export resume.
- Four cloud workers by default; one audio encode at a time and one shared,
  serialized Whisper fallback model. Cloud concurrency is per job, not a global
  multi-user Azure quota scheduler.
- Transient HTTP/network errors retry with backoff and Retry-After. A cooldown
  beyond the configured timeout uses fallback instead of retrying early.
  Authentication failures stop further cloud attempts. Secrets and response bodies
  are omitted from failure logs; credential-bearing redirects are disabled.
- Temporary MP3s are removed even on failures. Normalized caches remain under
  the source folder's `.mai_transcript_cache`; original media is preserved.

## Setup

Create an Azure Speech/Foundry resource in a supported MAI region. After the
current VOD finishes, restart using your existing launcher. Open Admin and use
the new Transcription provider form: choose MAI, paste your resource endpoint and
key, leave concurrency at 4, then save. New jobs use the setting. No key is shown
back in the form. Do not send your key in chat or include it in a shared ZIP.

Alternatively add these values to your current `.env` and restart afterward:

```dotenv
TRANSCRIPTION_PROVIDER=mai
TRANSCRIPTION_FALLBACK=whisper
AZURE_SPEECH_ENDPOINT=https://YOUR-RESOURCE.cognitiveservices.azure.com
AZURE_SPEECH_KEY=YOUR_KEY
MAI_MODEL=MAI-Transcribe-2
MAI_API_VERSION=2025-10-15
MAI_LANGUAGE=en
MAI_TRANSCRIBE_STYLE=verbatim
MAI_CHUNK_MINUTES=15
MAI_CHUNK_OVERLAP_SECONDS=8
MAI_CONCURRENCY=4
MAI_TIMEOUT_SECONDS=180
MAI_MAX_RETRIES=4
MAI_CACHE_ENABLED=true
MAI_USE_PRIORITY_NAMES=true
MAI_DIARIZATION=false
```

`MAI_MAX_RETRIES=4` means at most four total attempts per chunk. Use
`TRANSCRIPTION_FALLBACK=none` to fail instead of local recovery. Use
`TRANSCRIPTION_PROVIDER=whisper` to return to fully local ASR. Existing beam 1
settings remain. Blank `MAI_LANGUAGE` lets the provider detect language.

## Verification and limits

Automated tests exercise normalization, offsets, padding ownership, chronological
merge after out-of-order completion, cache reuse, long-VOD splitting, 429 retry,
authentication failure, chunk-only fallback, local/cloud facade behavior, silence,
malformed responses and reusable audio ingestion. Existing app tests are retained.
No Azure credentials were supplied, so funded MAI transcription, its actual speed,
recognition quality on Kick streams and account quota compatibility remain untested.
Test a representative 10–20 minute sample before switching long jobs to MAI.

Verification: 47 automated tests passed, Python syntax compilation passed, and an
isolated Admin integration check verified saving settings, secret non-disclosure
and worker configuration handoff. No current user job or database was used for tests.

Changed: transcribe.py, config.py, vod.py, app.py, benchmark.py, templates/admin.html,
.env.example, UPGRADE-NOTES.md. Added: mai_transcribe.py, audio_ingest_cache.py, tests and this guide.
The portable ZIP contains the complete source and assets, but excludes private
credentials, account databases, output media and transcription caches.
