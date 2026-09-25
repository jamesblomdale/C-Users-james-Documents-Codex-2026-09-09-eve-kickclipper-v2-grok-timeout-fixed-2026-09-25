# September 14 long-VOD pipeline

See LONG-VOD-UPGRADE.md for the integrated scheduler, progressive scout, updated requested scoring weights, tests and limitations. This supersedes older pipeline descriptions below.

# September 13 MAI upgrade

See MAI-UPGRADE.md for the optional parallel cloud transcription provider, chunk cache,
local fallback, setup, tests and limitations. The historical notes below predate MAI support.
47 automated tests and an isolated Admin integration check passed. No funded Azure request
has been made. Current jobs are not interrupted; restart after they finish to load new controls.

# September 11 review update

The new context review runs BEFORE final VOD ranking. It reviews at most ten candidates by default (Admin: 0–20), includes 90 seconds before and 30 seconds after as labelled context, separates literal events from social interpretations, and includes a skeptical edit assessment. Evidence quotes are checked against nearby transcript words. Valid, sufficiently confident reviews may change a score by at most 12 points. This is an editorial heuristic, not a measured accuracy improvement or independent panel of models.

Optional Gemini candidate-video review uses the documented Files API and requests AGENTIC media processing with video and audio. It limits each candidate to 240 seconds and its proxy to 40 MB, records whether a navigation trace was returned, and attempts upload deletion afterward. It requires a separately configured GEMINI_API_KEY. No paid Gemini call was made during verification. Frame review remains available with your existing provider. Neither path searches all silent moments across the VOD.

Review limits constrain requests, not currency. Local transcription remains the budget default. MAI-Transcribe-2, full diarization, automatic historical learning, embeddings, and whole-VOD visual event discovery are not implemented in this release. They need separate provider configuration and evaluation; adding all of them by default would conflict with the speed and budget requirement. No system here guarantees perfect transcripts or eight hours processed in thirty minutes.

The caption-path fix and accurate export counts remain included. Website copy now distinguishes local processing, provider analysis, clip review and manual publication. Existing clip sources are reused only if ffprobe finds playable video.

Verified this revision: 28 unit tests, Python compilation, authenticated dashboard/admin rendering, editor validation and clip deletion routes. The browser loaded the revised landing page; the automated browser harness was blocked by Windows subprocess permissions in this session. Funded provider quality, long VOD speed and multi-speaker tracking accuracy remain unverified.

Research checked against primary documentation:
- Google video processing: https://ai.google.dev/gemini-api/docs/generate-content/video-understanding
- Google upload and cleanup: https://ai.google.dev/gemini-api/docs/files
- Microsoft MAI transcription: https://learn.microsoft.com/en-us/azure/ai-services/speech-service/mai-transcribe

The following notes describe the earlier upgrade and its measurements; earlier statements about visual review timing and installation status are historical.

# Kick Clipper upgrade

This is an updated local Flask/Python app. The original ZIP is unchanged. Private keys, the existing account database and original videos are not included in the delivery archive.

## What changed

- **Log Guardian:** every running job gets a dedicated local monitor. It flags errors, retries, quota/credential/compute problems and long gaps in output. It gives a diagnosis hint and keeps incidents visible beside progress. It does not secretly rewrite your code or promise to catch every possible defect. Logs remain persisted through the existing job-event database.
- **Stage ETA:** rolling measured progress replaces unlabelled estimates. It resets at stage changes, displays a calibration state and explicitly says the estimate covers the current stage. A reliable whole-job ETA needs measurements from comparable completed VODs; this release does not pretend to have them.
- **Transcription:** configurable batch and beam sizes, GPU detection without downloading a second model, word timestamps, confidence warnings, saved VOD transcripts and fatal errors instead of declaring failed decoding to be silence. Turbo and Large v3 are available in the model selectors.
- **Discovery:** brief punchlines reach the scorer; eight categories replace the X-only outrage gates. Scanning all windows continues even when an export limit is selected. Window lookup uses indexed timestamps. Repeated jobs have unique work folders and clip names.
- **Visual context:** an optional vision model in Admin reviews six frames per shortlisted clip. The review is saved beside the clip and shown in the library. It describes visible evidence and uncertainty; it is not continuous audiovisual understanding, speaker diarization or discovery of silent visual moments.
- **Editing:** saved values load correctly; numeric/enum inputs are validated; replacement video is committed after a successful render; concurrent edits are rejected rather than racing. Windows-safe Unicode overlays replace the hardcoded Linux font path. Captions are applied after the tracking crop. Live clips retain source media and recipes for future edits. Added 4:5 tracking support.
- **Tracking:** crop changes cut directly at an observed speaker switch, with short hold time and smoothing within a shot. Face assignment cannot reuse one track twice in a frame. Lower-face motion is still a heuristic, not a validated active-speaker model.
- **Packaging:** separate YouTube/TikTok titles, a structured X post, overlay title and opening hook; refresh titles from the clip card. Evidence-bound prompts discourage invented reactions, identities and quotes.
- **Library/design:** reversible deletion into the user's `.trash` folder; dark violet surfaces, restrained highlights, raised controls and a perspective timeline banner. Existing routes and workflow are retained.

## Second opinion on the old ratings

The old scorer weighted judgment and comment potential at 40% each and imposed hard ceilings when either was low. This is an editorial preference for confrontational X content, not a general model of good short video. A funny payoff or impressive skill could never recover from those gates. Keyword prefilters amplified missed moments; limiting discovery after early candidates made later parts of long streams invisible.

The new weights are hook 20%, payoff 20%, standalone clarity 15%, emotion 15%, novelty 10%, judgment 8%, discussion potential 7%, technical 5%. These are starting editorial weights, not proven predictions. Validate them with a labelled set of moments and your own watch-through, retention, shares and results per platform. Hold out entire VODs when evaluating changes so overlapping windows do not make accuracy look better than it is. Do not interpret 85/100 as an 85% chance of going viral.

YouTube documents viewer choice, average view duration and average percentage viewed as Shorts ranking signals. That does not support the pasted claim that titles/search alone drive most Shorts views. [YouTube: Shorts search and discovery](https://support.google.com/youtube/answer/11914225?hl=en)

TikTok says user interactions, including time spent watching, generally receive more weight than other factors. Specific content and audience response matter; a universal three-word trick is not established. [TikTok: recommendations](https://support.tiktok.com/en/using-tiktok/exploring-videos/how-tiktok-recommends-content)

The pasted X multipliers and blanket claim about external links were not treated as reliable constants. None are encoded here. The examples supplied demonstrate possible angles, but view counts without impressions, account size, publication time and retention cannot establish why a video succeeded. No current creator-leaderboard or trend feed is connected.

## Eight hours within thirty minutes

Eight hours in thirty minutes requires **16× real-time end to end**. The existing audio-first approach is appropriate: obtain audio, transcribe once, scan overlapping text windows in parallel, then fetch/render only selected video spans. The implementation now supports batching and avoids repeated full-word scans; selecting tiny/beam-1 solely for speed would not satisfy the accuracy requirement.

The faster-whisper maintainers report 13 minutes of audio transcribed in 17 seconds with batch 8, large-v2, FP16 on an RTX 3070 Ti 8 GB. A simple linear extrapolation is roughly 10.5 minutes of transcription for eight hours, before download, API calls, model loading or rendering. It is not a benchmark of your machine or this app. CPU performance is substantially different. [Source and CUDA requirements](https://github.com/SYSTRAN/faster-whisper)

This machine's CTranslate2 runtime reported **0 CUDA devices** during checks. A real 60-second sample from the existing N3on VOD took **8.49 seconds** with the installed small model, CPU INT8, batch 1 and beam 3 (model load: 3.61 seconds). That is **7.06× real time**, or approximately **68 minutes for transcription alone** if the rate held across eight hours. This is a short-sample extrapolation, not a completed VOD benchmark or an accuracy evaluation. Thirty-minute completion has not been demonstrated. For accuracy-oriented speed, benchmark the same representative 10–20 minute noisy streamer sample with `large-v3` and `turbo`, compare transcripts manually, and choose the batch size that fits GPU memory. No hardware or service was purchased.

Run `python benchmark.py path-to-sample.mp4 --model turbo` in the configured environment. It writes measured throughput and an explicitly ASR-only eight-hour extrapolation. Count the rest of the pipeline separately. Full-VOD transcription must not be confused with exporting dozens of tracked 1080p clips.

## Setup and verification

Use a short Windows directory, retain your existing `.env`, database and media, then run `Setup.ps1`. It installs requirements and Playwright Chromium. Start with `Start.ps1`; open `http://127.0.0.1:5000`. Do not run a second copy on the same port. Provider credentials and owner login belong only in `.env`/Admin.

New settings: `WHISPER_BATCH_SIZE` (1 CPU / try 8 GPU), `WHISPER_BEAM_SIZE` (3 default), `WHISPER_DEVICE=cpu` to force CPU, optional `WHISPER_LANGUAGE=en`, and `VISION_MODEL` (blank disables frame review). Frame review uses the same configured provider/key and adds API cost; select a currently supported vision model. [xAI image input](https://docs.x.ai/developers/model-capabilities/images/understanding)

The default Grok scoring model was changed from the obsolete `grok-4-1-fast` slug to `grok-4.3`; an existing explicit `.env` value still overrides it. Check old settings before a run. [xAI model retirement guidance](https://docs.x.ai/developers/migration/may-15-retirement)

Verified: 22 billing and focused scoring/ETA/validation/tracking/provider-failure tests, route isolation/validation checks, Python compilation, dashboard/admin JavaScript syntax, real FFmpeg caption/overlay exports with and without tracking, one local CPU transcription benchmark, and browser checks of login, saved editor settings, overlay re-render and reversible clip deletion.

Not verified: a real eight-hour VOD, live capture end to end, provider-funded vision/title calls, real two-person active-speaker accuracy, or GPU processing. A normal pip upgrade was blocked by environment permissions; the verified upstream 1.2.1 wheel was subsequently bundled and exercised successfully with the existing native dependencies.

## Actual log finding

The existing database's September 9 run contains repeated Grok 403 errors saying the account has exhausted available credits or reached its monthly spending limit. The app dropped scoring windows and then recorded `completed_empty`. This is a provider failure, not evidence that the VOD lacked worthwhile clips. The upgrade caches fatal authentication/billing failures, stops VOD scoring on them and reports missing-content failures instead of quietly presenting an empty success. No credits were purchased and no spending limit was changed.

The original app was still listening on port 5000 during handoff. It has not been stopped or overwritten. Stop the original app and active processing jobs, then run `Install-Upgrade.ps1` from the extracted upgrade folder. It backs up replaced source files and preserves `.env`, the account database, model cache and output clips. Then run `Start.cmd` in the original folder. The installer refuses to replace a running app.

## Included files and measured batch mode

The 3D library is now local (`static/vendor/three.module.min.js`) with its MIT license. The verified upstream faster-whisper 1.2.1 wheel is bundled under `vendor_python` with its license and SHA-256 provenance. `transcribe.py` loads it ahead of an older installation. Native dependencies still come from the existing environment or `Setup.ps1`; this archive is not a full Python/GPU runtime or a multi-gigabyte model download.

On the same real 60-second N3on sample, bundled 1.2.1 with small/CPU INT8/batch 4/beam 3 took **7.58 seconds** (load: 4.10 seconds), or **7.91× real time**. Linear ASR-only extrapolation: **60.7 minutes** for eight hours. A low-confidence warning was surfaced. These small-sample timings do not establish transcription accuracy equivalence; inspect real words against the audio before adopting a faster profile.

The VOD pipeline now checks the scoring provider before downloading or transcribing, so an exhausted provider account can stop immediately instead of wasting a full transcription pass.
